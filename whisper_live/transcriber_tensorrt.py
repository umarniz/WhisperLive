import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Union

import torch
import numpy as np
import torch.nn.functional as F
from whisper_live.tokenizer import get_tokenizer
from whisper_live.tensorrt_utils import (N_SAMPLES, mel_filters, load_audio_wav_format, pad_or_trim, load_audio, log_mel_spectrogram)

import tensorrt_llm
import tensorrt_llm.logger as logger
from tensorrt_llm._utils import (str_dtype_to_torch, str_dtype_to_trt,
                                 trt_dtype_to_torch)
from tensorrt_llm.runtime import ModelConfig, SamplingConfig
from tensorrt_llm.runtime.session import Session, TensorInfo


def remove_tensor_padding(input_tensor, input_tensor_lengths=None, pad_value=0):
    if input_tensor.dim() == 2:
        # Text tensor case: batch, seq_len
        assert torch.all(
            input_tensor[:, 0] != pad_value
        ), "First token in each sequence should not be pad_value"
        assert input_tensor_lengths is None

        # Create a mask for all non-pad tokens
        mask = input_tensor != pad_value

        # Apply the mask to input_tensor to remove pad tokens
        output_tensor = input_tensor[mask].view(1, -1)

    elif input_tensor.dim() == 3:
        # Audio tensor case: batch, seq_len, feature_len
        assert input_tensor_lengths is not None, "input_tensor_lengths must be provided for 3D input_tensor"
        batch_size, seq_len, feature_len = input_tensor.shape

        # Initialize a list to collect valid sequences
        valid_sequences = []

        for i in range(batch_size):
            valid_length = input_tensor_lengths[i]
            valid_sequences.append(input_tensor[i, :valid_length, :])

        # Concatenate all valid sequences along the batch dimension
        output_tensor = torch.cat(valid_sequences, dim=0)

    else:
        raise ValueError("Input tensor must have 2 or 3 dimensions")

    return output_tensor


def read_config(component, engine_dir):
    config_path = engine_dir / component / 'config.json'
    with open(config_path, 'r') as f:
        config = json.load(f)
    model_config = OrderedDict()
    model_config.update(config['pretrained_config'])
    model_config.update(config['build_config'])
    return model_config


class WhisperEncoding:

    def __init__(self, engine_dir):
        self.session = self.get_session(engine_dir)
        config = read_config('encoder', engine_dir)
        self.n_mels = config['n_mels']
        self.dtype = config['dtype']
        self.num_languages = config['num_languages']
        self.encoder_config = config

    def get_session(self, engine_dir):
        serialize_path = engine_dir / 'encoder' / 'rank0.engine'
        with open(serialize_path, 'rb') as f:
            session = Session.from_serialized_engine(f.read())
        return session

    def get_audio_features(self, mel):
        # Input_lengths here are actually encoder_output_lengths for whisper.
        # Since the conv subsampling layer in the whisper decoder, seq_len would divide by 2.
        input_lengths = torch.tensor(
            [mel.shape[2] // 2 for _ in range(mel.shape[0])],
            dtype=torch.int32,
            device=mel.device)
        encoder_max_input_length = torch.max(input_lengths).item()
        if self.encoder_config['plugin_config']['remove_input_padding']:
            mel_input_lengths = torch.full((mel.shape[0], ),
                                           mel.shape[2],
                                           dtype=torch.int32,
                                           device='cuda')
            # mel B,D,T -> B,T,D -> BxT, D
            mel = mel.transpose(1, 2)
            mel = remove_tensor_padding(mel, mel_input_lengths)

        inputs = OrderedDict()
        inputs['input_features'] = mel
        inputs['input_lengths'] = input_lengths

        output_list = [
            TensorInfo('input_features', str_dtype_to_trt(self.dtype),
                       mel.shape),
            TensorInfo('input_lengths', str_dtype_to_trt('int32'),
                       input_lengths.shape)
        ]

        output_info = (self.session).infer_shapes(output_list)

        logger.debug(f'output info {output_info}')
        outputs = {
            t.name: torch.empty(tuple(t.shape),
                                dtype=trt_dtype_to_torch(t.dtype),
                                device='cuda')
            for t in output_info
        }
        stream = torch.cuda.current_stream()
        ok = self.session.run(inputs=inputs,
                              outputs=outputs,
                              stream=stream.cuda_stream)
        assert ok, 'Engine execution failed'
        stream.synchronize()
        audio_features = outputs['encoder_output']
        return audio_features, encoder_max_input_length, input_lengths


class WhisperDecoding:

    def __init__(self, engine_dir, runtime_mapping, debug_mode=False):

        self.decoder_config = read_config('decoder', engine_dir)
        self.decoder_generation_session = self.get_session(
            engine_dir, runtime_mapping, debug_mode)

    def get_session(self, engine_dir, runtime_mapping, debug_mode=False):
        serialize_path = engine_dir / 'decoder' / 'rank0.engine'
        with open(serialize_path, "rb") as f:
            decoder_engine_buffer = f.read()

        decoder_model_config = ModelConfig(
            max_batch_size=self.decoder_config['max_batch_size'],
            max_beam_width=self.decoder_config['max_beam_width'],
            num_heads=self.decoder_config['num_attention_heads'],
            num_kv_heads=self.decoder_config['num_attention_heads'],
            hidden_size=self.decoder_config['hidden_size'],
            vocab_size=self.decoder_config['vocab_size'],
            cross_attention=True,
            num_layers=self.decoder_config['num_hidden_layers'],
            gpt_attention_plugin=self.decoder_config['plugin_config']
            ['gpt_attention_plugin'],
            remove_input_padding=self.decoder_config['plugin_config']
            ['remove_input_padding'],
            paged_kv_cache=self.decoder_config['plugin_config']
            ['paged_kv_cache'],
            has_position_embedding=self.
            decoder_config['has_position_embedding'],
            dtype=self.decoder_config['dtype'],
            has_token_type_embedding=False,
        )
        decoder_generation_session = tensorrt_llm.runtime.GenerationSession(
            decoder_model_config,
            decoder_engine_buffer,
            runtime_mapping,
            debug_mode=debug_mode)

        return decoder_generation_session

    def generate(self,
                 decoder_input_ids,
                 encoder_outputs,
                 encoder_max_input_length,
                 encoder_input_lengths,
                 eot_id,
                 max_new_tokens=40,
                 num_beams=1):
        batch_size = decoder_input_ids.shape[0]
        decoder_input_lengths = torch.tensor([
            decoder_input_ids.shape[-1]
            for _ in range(decoder_input_ids.shape[0])
        ],
                                             dtype=torch.int32,
                                             device='cuda')
        decoder_max_input_length = torch.max(decoder_input_lengths).item()

        cross_attention_mask = torch.ones(
            [batch_size, 1, encoder_max_input_length]).int().cuda()

        # generation config
        sampling_config = SamplingConfig(end_id=eot_id,
                                         pad_id=eot_id,
                                         num_beams=num_beams)
        self.decoder_generation_session.setup(
            decoder_input_lengths.size(0),
            decoder_max_input_length,
            max_new_tokens,
            beam_width=num_beams,
            encoder_max_input_length=encoder_max_input_length)

        torch.cuda.synchronize()

        decoder_input_ids = decoder_input_ids.type(torch.int32).cuda()
        if self.decoder_config['plugin_config']['remove_input_padding']:
            # 50256 is the index of <pad> for all whisper models' decoder
            WHISPER_PAD_TOKEN_ID = 50256
            decoder_input_ids = remove_tensor_padding(
                decoder_input_ids, pad_value=WHISPER_PAD_TOKEN_ID)
            if encoder_outputs.dim() == 3:
                encoder_output_lens = torch.full((encoder_outputs.shape[0], ),
                                                 encoder_outputs.shape[1],
                                                 dtype=torch.int32,
                                                 device='cuda')

                encoder_outputs = remove_tensor_padding(encoder_outputs,
                                                        encoder_output_lens)
        output_ids = self.decoder_generation_session.decode(
            decoder_input_ids,
            decoder_input_lengths,
            sampling_config,
            encoder_output=encoder_outputs,
            encoder_input_lengths=encoder_input_lengths,
            cross_attention_mask=cross_attention_mask,
        )
        torch.cuda.synchronize()

        # get the list of int from output_ids tensor
        output_ids = output_ids.cpu().numpy().tolist()
        return output_ids


class WhisperTRTLLM(object):

    def __init__(self, engine_dir, debug_mode=False, assets_dir=None):
        world_size = 1
        runtime_rank = tensorrt_llm.mpi_rank()
        runtime_mapping = tensorrt_llm.Mapping(world_size, runtime_rank)
        torch.cuda.set_device(runtime_rank % runtime_mapping.gpus_per_node)
        engine_dir = Path(engine_dir)

        self.encoder = WhisperEncoding(engine_dir)
        self.decoder = WhisperDecoding(engine_dir,
                                       runtime_mapping,
                                       debug_mode=False)
        is_multilingual = (self.decoder.decoder_config['vocab_size'] >= 51865)
        if is_multilingual:
            tokenizer_name = "multilingual"
            assert (Path(assets_dir) / "multilingual.tiktoken").exists(
            ), "multilingual.tiktoken file is not existed in assets_dir"
        else:
            tokenizer_name = "gpt2"
            assert (Path(assets_dir) / "gpt2.tiktoken").exists(
            ), "gpt2.tiktoken file is not existed in assets_dir"
        self.tokenizer = get_tokenizer(name=tokenizer_name,
                                       num_languages=self.encoder.num_languages,
                                       tokenizer_dir=assets_dir)
        self.eot_id = self.tokenizer.encode(
            "<|endoftext|>",
            allowed_special=self.tokenizer.special_tokens_set)[0]

        print('Successfully initialized WhisperTRTLLM', 'mels', self.encoder.n_mels, 'tokenizer', self.tokenizer)


    def process_batch(
            self,
            mel,
            text_prefix="<|startoftranscript|><|en|><|transcribe|><|notimestamps|>",
            num_beams=1):
        prompt_id = self.tokenizer.encode(
            text_prefix, allowed_special=self.tokenizer.special_tokens_set)
        prompt_id = torch.tensor(prompt_id)
        batch_size = mel.shape[0]
        decoder_input_ids = prompt_id.repeat(batch_size, 1)


        encoder_output, encoder_max_input_length, encoder_input_lengths = self.encoder.get_audio_features(
            mel)
        output_ids = self.decoder.generate(decoder_input_ids,
                                           encoder_output,
                                           encoder_max_input_length,
                                           encoder_input_lengths,
                                           self.eot_id,
                                           max_new_tokens=96,
                                           num_beams=num_beams)
        texts = []
        for i in range(len(output_ids)):
            text = self.tokenizer.decode(output_ids[i][0]).strip()
            texts.append(text)
        return texts

    def log_mel_spectrogram_on_model(self, wave, return_duration=False):
        return log_mel_spectrogram(wave, self.encoder.n_mels, device='cuda', return_duration=return_duration)

    def transcribe(
        self,
        mel,
        text_prefix="<|startoftranscript|><|en|><|transcribe|><|notimestamps|>",
        dtype='float16',
        batch_size=1,
        num_beams=1,
        ):

        mel = mel.type(str_dtype_to_torch(dtype))
        mel = mel.unsqueeze(0)
        predictions = self.process_batch(mel, text_prefix, num_beams)
        prediction = predictions[0]
        # remove all special tokens in the prediction
        prediction = re.sub(r'<\|.*?\|>', '', prediction)
        return prediction.strip()

def decode_wav_file(
        input_file_path,
        model,
        text_prefix="<|startoftranscript|><|en|><|transcribe|><|notimestamps|>",
        dtype='float16',
        batch_size=4,
        num_beams=1,
        normalizer=None,
        mel_filters_dir=None):
    mel, total_duration = log_mel_spectrogram(input_file_path,
                                              model.encoder.n_mels,
                                              device='cuda',
                                              return_duration=True,
                                              mel_filters_dir=mel_filters_dir)
    mel = mel.type(str_dtype_to_torch(dtype))
    mel = mel.unsqueeze(0)
    # repeat the mel spectrogram to match the batch size
    mel = mel.repeat(batch_size, 1, 1)
    predictions = model.process_batch(mel, text_prefix, num_beams)
    prediction = predictions[0]

    # remove all special tokens in the prediction
    prediction = re.sub(r'<\|.*?\|>', '', prediction)
    if normalizer:
        prediction = normalizer(prediction)
    print(f"prediction: {prediction}")
    results = [(0, [""], prediction.split())]
    return results, total_duration

def collate_wrapper(batch):
    speeches, durations, labels, ids = [], [], [], []
    for item in batch:
        speech = item["audio"]["array"]
        duration = speech.shape[-1]
        speech = pad_or_trim(speech, N_SAMPLES)
        speech = speech.astype(np.float32)
        speech = torch.from_numpy(speech)
        speeches.append(speech)
        durations.append(duration)
        labels.append(item["text"])
        ids.append(item["id"])
    return speeches, durations, labels, ids


def decode_dataset(
        model,
        dataset,
        text_prefix="<|startoftranscript|><|en|><|transcribe|><|notimestamps|>",
        dtype='float16',
        batch_size=1,
        num_beams=1,
        normalizer=None,
        sample_rate=16000,
        mel_filters_dir=None):
    librispeech_dummy = load_dataset(dataset, "clean", split="validation")

    data_loader = DataLoader(librispeech_dummy,
                             batch_size=batch_size,
                             num_workers=4,
                             pin_memory=True,
                             collate_fn=collate_wrapper)
    results = []
    total_duration = 0
    for batch in data_loader:
        waveforms, durations, texts, ids = batch
        total_duration += sum(durations) / sample_rate

        for wave in waveforms:
            assert wave.is_pinned()

        features = [
            log_mel_spectrogram(wave,
                                model.encoder.n_mels,
                                device='cuda',
                                mel_filters_dir=mel_filters_dir).unsqueeze(0)
            for wave in waveforms
        ]
        features = torch.cat(features, dim=0).type(str_dtype_to_torch(dtype))
        predictions = model.process_batch(features, text_prefix, num_beams)
        for wav_id, label, prediction in zip(ids, texts, predictions):
            # remove all special tokens in the prediction
            prediction = re.sub(r'<\|.*?\|>', '', prediction)
            if normalizer:
                prediction, label = normalizer(prediction), normalizer(label)
            print(f"wav_id: {wav_id}, label: {label}, prediction: {prediction}")
            results.append((wav_id, label.split(), prediction.split()))
    return results, total_duration
