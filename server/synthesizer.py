import io
import re
import sys
import time

import numpy as np
import torch
import yaml

from TTS.utils.audio import AudioProcessor
from TTS.utils.io import load_config
from TTS.utils.generic_utils import setup_model
from TTS.utils.speakers import load_speaker_mapping
from TTS.vocoder.utils.generic_utils import setup_generator
# pylint: disable=unused-wildcard-import
# pylint: disable=wildcard-import
from TTS.utils.synthesis import *

from TTS.utils.text import make_symbols, phonemes, symbols

alphabets = r"([A-Za-z])"
prefixes = r"(Mr|St|Mrs|Ms|Dr)[.]"
suffixes = r"(Inc|Ltd|Jr|Sr|Co)"
starters = r"(Mr|Mrs|Ms|Dr|He\s|She\s|It\s|They\s|Their\s|Our\s|We\s|But\s|However\s|That\s|This\s|Wherever)"
acronyms = r"([A-Z][.][A-Z][.](?:[A-Z][.])?)"
websites = r"[.](com|net|org|io|gov)"


class Synthesizer(object):
    def __init__(self, config):
        self.wavernn = None
        self.pwgan = None
        self.config = config
        self.use_cuda = self.config.use_cuda
        if self.use_cuda:
            assert torch.cuda.is_available(), "CUDA is not availabe on this machine."
        self.load_tts(self.config.tts_checkpoint, self.config.tts_config,
                      self.config.use_cuda)
        if self.config.vocoder_checkpoint:
            self.load_vocoder(self.config.vocoder_checkpoint, self.config.vocoder_config, self.config.use_cuda)
        if self.config.wavernn_lib_path:
            self.load_wavernn(self.config.wavernn_lib_path, self.config.wavernn_file,
                              self.config.wavernn_config, self.config.use_cuda)
        if self.config.pwgan_file:
            self.load_pwgan(self.config.pwgan_lib_path, self.config.pwgan_file,
                            self.config.pwgan_config, self.config.use_cuda)

    def load_tts(self, tts_checkpoint, tts_config, use_cuda):
        # pylint: disable=global-statement
        global symbols, phonemes

        print(" > Loading TTS model ...")
        print(" | > model config: ", tts_config)
        print(" | > checkpoint file: ", tts_checkpoint)

        self.tts_config = load_config(tts_config)
        self.use_phonemes = self.tts_config.use_phonemes
        self.ap = AudioProcessor(**self.tts_config.audio)

        if 'characters' in self.tts_config.keys():
            symbols, phonemes = make_symbols(**self.tts_config.characters)

        if self.use_phonemes:
            self.input_size = len(phonemes)
        else:
            self.input_size = len(symbols)
        # TODO: fix this for multi-speaker model - load speakers
        if self.config.tts_speakers is not None:
            self.tts_speakers = load_speaker_mapping(self.config.tts_speakers)
            num_speakers = len(self.tts_speakers)
        else:
            num_speakers = 0
        self.tts_model = setup_model(self.input_size, num_speakers=num_speakers, c=self.tts_config)
        # load model state
        cp = torch.load(tts_checkpoint, map_location=torch.device('cpu'))
        # load the model
        self.tts_model.load_state_dict(cp['model'])
        if use_cuda:
            self.tts_model.cuda()
        self.tts_model.eval()
        self.tts_model.decoder.max_decoder_steps = 3000
        if 'r' in cp:
            self.tts_model.decoder.set_r(cp['r'])
            print(f" > model reduction factor: {cp['r']}")

    def load_vocoder(self, model_file, model_config, use_cuda):
        self.vocoder_config = load_config(model_config)
        self.vocoder_model = setup_generator(self.vocoder_config)
        self.vocoder_model.load_state_dict(torch.load(model_file, map_location="cpu")["model"])
        self.vocoder_model.remove_weight_norm()
        self.vocoder_model.inference_padding = 0
        self.vocoder_config = load_config(model_config)

        if use_cuda:
            self.vocoder_model.cuda()
        self.vocoder_model.eval()

    def load_wavernn(self, lib_path, model_file, model_config, use_cuda):
        # TODO: set a function in wavernn code base for model setup and call it here.
        sys.path.append(lib_path) # set this if WaveRNN is not installed globally
        #pylint: disable=import-outside-toplevel
        from WaveRNN.models.wavernn import Model
        print(" > Loading WaveRNN model ...")
        print(" | > model config: ", model_config)
        print(" | > model file: ", model_file)
        self.wavernn_config = load_config(model_config)
        # This is the default architecture we use for our models.
        # You might need to update it
        self.wavernn = Model(
            rnn_dims=512,
            fc_dims=512,
            mode=self.wavernn_config.mode,
            mulaw=self.wavernn_config.mulaw,
            pad=self.wavernn_config.pad,
            use_aux_net=self.wavernn_config.use_aux_net,
            use_upsample_net=self.wavernn_config.use_upsample_net,
            upsample_factors=self.wavernn_config.upsample_factors,
            feat_dims=80,
            compute_dims=128,
            res_out_dims=128,
            res_blocks=10,
            hop_length=self.ap.hop_length,
            sample_rate=self.ap.sample_rate,
        ).cuda()

        check = torch.load(model_file, map_location="cpu")
        self.wavernn.load_state_dict(check['model'])
        if use_cuda:
            self.wavernn.cuda()
        self.wavernn.eval()

    def load_pwgan(self, lib_path, model_file, model_config, use_cuda):
        if lib_path:
            # set this if ParallelWaveGAN is not installed globally
            sys.path.append(lib_path)
        try:
            #pylint: disable=import-outside-toplevel
            from parallel_wavegan.models import ParallelWaveGANGenerator
        except ImportError as e:
            raise RuntimeError(f"cannot import parallel-wavegan, either install it or set its directory using the --pwgan_lib_path command line argument: {e}")
        print(" > Loading PWGAN model ...")
        print(" | > model config: ", model_config)
        print(" | > model file: ", model_file)
        with open(model_config) as f:
            self.pwgan_config = yaml.load(f, Loader=yaml.Loader)
        self.pwgan = ParallelWaveGANGenerator(**self.pwgan_config["generator_params"])
        self.pwgan.load_state_dict(torch.load(model_file, map_location="cpu")["model"]["generator"])
        self.pwgan.remove_weight_norm()
        if use_cuda:
            self.pwgan.cuda()
        self.pwgan.eval()

    def save_wav(self, wav, path):
        # wav *= 32767 / max(1e-8, np.max(np.abs(wav)))
        wav = np.array(wav)
        self.ap.save_wav(wav, path)

    @staticmethod
    def split_into_sentences(text):
        text = " " + text + "  <stop>"
        text = text.replace("\n", " ")
        text = re.sub(prefixes, "\\1<prd>", text)
        text = re.sub(websites, "<prd>\\1", text)
        if "Ph.D" in text:
            text = text.replace("Ph.D.", "Ph<prd>D<prd>")
        text = re.sub(r"\s" + alphabets + "[.] ", " \\1<prd> ", text)
        text = re.sub(acronyms+" "+starters, "\\1<stop> \\2", text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]" + alphabets + "[.]", "\\1<prd>\\2<prd>\\3<prd>", text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]", "\\1<prd>\\2<prd>", text)
        text = re.sub(" "+suffixes+"[.] "+starters, " \\1<stop> \\2", text)
        text = re.sub(" "+suffixes+"[.]", " \\1<prd>", text)
        text = re.sub(" " + alphabets + "[.]", " \\1<prd>", text)
        if "”" in text:
            text = text.replace(".”", "”.")
        if "\"" in text:
            text = text.replace(".\"", "\".")
        if "!" in text:
            text = text.replace("!\"", "\"!")
        if "?" in text:
            text = text.replace("?\"", "\"?")
        text = text.replace(".", ".<stop>")
        text = text.replace("?", "?<stop>")
        text = text.replace("!", "!<stop>")
        text = text.replace("<prd>", ".")
        sentences = text.split("<stop>")
        sentences = sentences[:-1]
        sentences = list(filter(None, [s.strip() for s in sentences])) # remove empty sentences
        return sentences

    def tts(self, text, speaker_id=None):
        start_time = time.time()
        wavs = []
        sens = self.split_into_sentences(text)
        print(sens)
        speaker_id = id_to_torch(speaker_id)
        if speaker_id is not None and self.use_cuda:
            speaker_id = speaker_id.cuda()

        for sen in sens:
            # preprocess the given text
            inputs = text_to_seqvec(sen, self.tts_config)
            inputs = numpy_to_torch(inputs, torch.long, cuda=self.use_cuda)
            inputs = inputs.unsqueeze(0)
            # synthesize voice
            decoder_output, postnet_output, alignments, stop_tokens = run_model_torch(self.tts_model, inputs, self.tts_config, False, speaker_id, None)
            # convert outputs to numpy
            if self.vocoder_model:
                vocoder_input = postnet_output[0].transpose(0, 1).unsqueeze(0)
                wav = self.vocoder_model.inference(vocoder_input)
                if self.use_cuda:
                    wav = wav.cpu().numpy()
                else:
                    wav = wav.numpy()
                wav = wav.flatten()
            elif self.wavernn:
                vocoder_input = None
                if self.tts_config.model == "Tacotron":
                    vocoder_input = torch.FloatTensor(self.ap.out_linear_to_mel(linear_spec=postnet_output.T).T).T.unsqueeze(0)
                else:
                    vocoder_input = postnet_output[0].transpose(0, 1).unsqueeze(0)
                if self.use_cuda:
                    vocoder_input.cuda()
                wav = self.wavernn.generate(vocoder_input, batched=self.config.is_wavernn_batched, target=11000, overlap=550)
            # trim silence
            wav = trim_silence(wav, self.ap)

            wavs += list(wav)
            wavs += [0] * 10000

        out = io.BytesIO()
        self.save_wav(wavs, out)

        # compute stats
        process_time = time.time() - start_time
        audio_time = len(wavs) / self.tts_config.audio['sample_rate']
        print(f" > Processing time: {process_time}")
        print(f" > Real-time factor: {process_time / audio_time}")
        return out
