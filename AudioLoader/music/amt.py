import os
from pathlib import Path
from glob import glob
import shutil
import pickle
import numpy as np

# import soundfile
from tqdm import tqdm
import multiprocessing
from joblib import Parallel, delayed
from .utils import tsv2roll, check_md5, files, process_midi, process_csv
import torch
from torch.utils.data import Dataset
import warnings
import torchaudio

# the download utils were removed in torch==2.0
# __TORCH_GTE_2_0 = False
# split_version = torch.__version__.split(".")
# major_version = int(split_version[0])
# if major_version > 1:
#     __TORCH_GTE_2_0 = True
#     from torchaudio.datasets.utils import _extract_zip as extract_archive
#     from torch.hub import download_url_to_file as download_url
# else:
#     from torchaudio.datasets.utils import (
#         download_url,
#         extract_archive,
#     )
from AudioLoader.utils import (
    download_url,
    extract_archive,
)
from collections import OrderedDict
import math

import json
import mido
import importlib
from importlib import (
    resources,
)  # need this one to prevent the error AttributeError: module 'importlib' has no attribute 'resources'

"""
This file is based on https://github.com/jongwook/onsets-and-frames

TODA:
1. Don't load everything on RAM, load only the files when needed (or only do it with MAESTRO?)
2. 
"""


class AMTDataset(Dataset):
    def __init__(
        self,
        use_cache=False,
        download=True,
        preload=False,
        sequence_length=None,
        seed=42,
        hop_length=512,
        max_midi=108,
        min_midi=21,
        ext_audio=".wav",
    ):

        self.use_cache = use_cache
        self.download = download
        self.preload = preload
        self.sequence_length = sequence_length
        self.random = np.random.RandomState(seed)
        self.hop_length = hop_length
        self.max_midi = max_midi
        self.min_midi = min_midi
        self.ext_audio = ext_audio

    def load(self, index):
        """
        load an audio track and the corresponding labels
        Returns
        -------
            A dictionary containing the following data:
            path: str
                the path to the audio file
            audio: torch.ShortTensor, shape = [num_samples]
                the raw waveform
            label: torch.ByteTensor, shape = [num_steps, midi_bins]
                a matrix that contains the onset/offset/frame labels encoded as:
                3 = onset, 2 = frames after onset, 1 = offset, 0 = all else
            velocity: torch.ByteTensor, shape = [num_steps, midi_bins]
                a matrix that contains MIDI velocity values at the frame locations
        """
        if self.dataset == "MAESTRO":
            audio_path, tsv_path = self._walker[index]
        elif self.dataset == "MusicNet" or self.dataset == "MAPS":
            audio_path = self._walker[index]
            if self.sampling_rate and (self.sampling_rate != 44100):
                tsv_path = audio_path.replace(".flac", ".tsv")
            else:
                tsv_path = audio_path.replace(self.ext_audio, ".tsv")
        #             saved_data_path = audio_path.replace(self.ext_audio, '.pt')
        #             if os.path.exists(audio_path.replace(self.ext_audio, '.pt')) and self.use_cache==True:
        # Check if .pt files exist, if so just load the files
        #                 return torch.load(saved_data_path)
        # Otherwise, create the .pt files
        waveform, sr = torchaudio.load(audio_path)
        if waveform.dim() == 2:
            waveform = waveform.mean(0)  # converting a stereo track into a mono track
        audio_length = len(waveform)

        tsv = np.loadtxt(tsv_path, delimiter="\t", skiprows=1)

        pianoroll, velocity_roll = tsv2roll(
            tsv, audio_length, sr, self.hop_length, max_midi=108, min_midi=21
        )
        data = dict(
            path=audio_path,
            sr=sr,
            audio=waveform,
            tsv=tsv,
            pianoroll=pianoroll,
            velocity_roll=velocity_roll,
        )

        #         if self.use_cache: # Only save new cache data in .pt format when use_cache==True
        #             torch.save(data, saved_data_path)
        return data

    def __getitem__(self, index):
        if self.preload:
            data = self._preloader[index]
            result = self.get_segment(data, self.hop_length, self.sequence_length)
            result["sr"] = data["sr"]
            return result
        else:
            data = self.load(index)
            result = self.get_segment(data, self.hop_length, self.sequence_length)
            result["sr"] = data["sr"]
            return result

    def get_segment(
        self, data, hop_size, sequence_length=None, max_midi=108, min_midi=21
    ):
        result = dict(path=data["path"])
        audio_length = len(data["audio"])
        pianoroll = data["pianoroll"]
        velocity_roll = data["velocity_roll"]
        #     start = time.time()
        #     pianoroll, velocity_roll = tsv2roll(data['tsv'], audio_length, data['sr'], hop_size, max_midi, min_midi)
        #     print(f'tsv2roll time used = {time.time()-start}')

        if sequence_length is not None:
            # slicing audio
            assert (
                audio_length - sequence_length
            ) > 0, f"sequence_length={sequence_length} is longer than the "
            f"audio_length={audio_length}. Please reduce the sequence_length"
            begin = self.random.randint(audio_length - sequence_length)
            #         begin = 1000 # for debugging
            end = begin + sequence_length
            result["audio"] = data["audio"][begin:end]

            # slicing pianoroll
            step_begin = begin // hop_size
            n_steps = sequence_length // hop_size

            step_end = step_begin + n_steps
            labels = pianoroll[step_begin:step_end, :]
            result["velocity"] = velocity_roll[step_begin:step_end, :]
        else:
            result["audio"] = data["audio"]
            labels = pianoroll
            result["velocity"] = velocity_roll

        #     result['audio'] = result['audio'].float().div_(32768.0) # converting to float by dividing it by 2^15
        result["onset"] = (labels == 3).float()
        result["offset"] = (labels == 1).float()
        result["frame"] = (labels > 1).float()
        result["velocity"] = (
            result["velocity"].float().div_(128.0)
        )  # not yet normalized
        # print(f"result['audio'].shape = {result['audio'].shape}")
        # print(f"result['label'].shape = {result['label'].shape}")
        return result

    def downsample_exist(self, output_format="flac"):
        if len(self._walker) == 0:
            return 0

        if self.dataset == "MAESTRO":
            for wavfile, tsvfile in tqdm(
                self._walker, desc=f"checking downsampled files"
            ):
                try:
                    dsampled_audio = wavfile.replace(
                        self.ext_audio, f".{output_format}"
                    )
                    assert os.path.isfile(
                        dsampled_audio
                    ), f"{dsampled_audio} is missing"
                except Exception as e:
                    warnings.warn(e.args[0])
                    return False
        elif self.dataset == "MusicNet" or self.dataset == "MAPS":
            for wavfile in tqdm(self._walker, desc=f"checking downsampled files"):
                try:
                    dsampled_audio = wavfile.replace(
                        self.ext_audio, f".{output_format}"
                    )
                    assert os.path.isfile(
                        dsampled_audio
                    ), f"{dsampled_audio} is missing"
                except Exception as e:
                    warnings.warn(e.args[0])
                    return False

        # if downsampled audio exist, check if the sr is correct.
        _, sr = torchaudio.load(dsampled_audio)
        try:
            assert (
                sr == self.sampling_rate
            ), f"{dsampled_audio} is having a sampling rate of {sr} instead of {self.sampling_rate}"
        except Exception as e:
            warnings.warn(e.args[0])
            return False

        return True

    #             print()

    def clear_caches(self):
        """ "Clearing existing .pt files"""
        cache_list = list(
            Path(os.path.join(self.root, self.name_archive)).rglob("*.pt")
        )
        decision = input(
            f"Found {len(cache_list)} .pt files"
            f"Do you want to remove them?"
            f"Choosing [no] if you want to double check the list of files to be removed when [yes/no]"
        )

        if decision.lower() == "yes":
            for file in cache_list:
                os.remove(file)
        elif decision.lower() == "no":
            return cache_list
        else:
            print(f"[{decision}] is not a supported answer. Clearing skipped.")
            return cache_list

    def __len__(self):
        return len(self._walker)


class MAPS(AMTDataset):
    def __init__(
        self,
        root="./",
        groups="all",
        data_type="MUS",
        overlap=False,
        sampling_rate=None,
        **kwargs,
    ):
        """
        This Dataset inherits from AMTDataset.

        Parameters
        ----------
        root: str
            The folder that contains the MAPS dataset folder

        groups: list or str
            Choose which sub-folders to load. Avaliable choices are
            `train`, `test`, `all`.
            Default: `all`, which stands for loading all sub-folders.
            Alternatively, users can provide a list of subfolders to be loaded.

        data_type: str
            Four different types of data are available, `MUS`, `ISOL`, `RAND`, `UCHO`.
            Default: `MUS`, which stands for full music pieces.

        overlap: bool
            TODO: To control if overlapping songs in the train set to be load.
            Default: False, which means that it will ignore audio clips in the train set
                     which already exist in the test set ('ENSTDkAm' and 'ENSTDkCl')

        use_cache: bool
            If it is set to `True`, the audio, piano roll and its metadata would be saved as a .pt file.
            Loading directly from the .pt files would be slightly faster than loading raw audio and tsv files
            Default:True

        download: bool
            To automatically download the dataset if it is set to `True`
            Default: True

        preload: bool
            When it is set to `True`, the data will be loaded into RAM, which makes loading faster.
            For large dataset, RAM memory might not be enough to store all the data, and we can
            use `preload=False` to read data on the fly.
            Default: False

        sequence_length: int
            The length of audio segment to be extracted.
            Since the audio is paired with a tsv file,
            changing this will automactially change the piano roll lenght too.
            Default: None

        seed: int
            This seed controls the segmentation indices, which allows the data loading to be reproducible.
            Default: 42

        hop_length: int
            It should be the same as the spectrogram hop_length,
            so that the piano roll timesteps will aligned with the spectrograms.
            Default: 512

        max_midi: int
            The highest MIDI note to be appeared on the piano roll
            Default: 108, which is equivalent to the highest note, C8, on a piano

        min_midi: int
            The lowest MIDI note to be appeared on the piano roll
            Default: 21, which is equivalent to the lowest note, A0, on a piano


        ext_audio: str
            The audio format to load. Most dataset provides audio in the format of `.wav` files.
            The `.resample(sr)` function resamples audio into `.flac` format.
            Therefore, changing the target audio format to be load can be used to control
            which set of audio data to use
            Default: '.wav'
        """

        self.overlap = overlap
        super().__init__(**kwargs)

        #         self.url = "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download"
        #         self.checksum = '02a8f140dc9a7c85639b0c01e5522add'

        self.url_dict = {
            "AkPnBcht": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=AkPnBcht.zip&downloadStartSecret=2qjs7gi2ixw",
            "AkPnBsdf": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=AkPnBsdf.zip&downloadStartSecret=lqjko9sgjv",
            "AkPnCGdD": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=AkPnCGdD.zip&downloadStartSecret=sqfhv2kjb4d",
            "AkPnStgb": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=AkPnStgb.zip&downloadStartSecret=p50p2c8wjka",
            "ENSTDkAm1": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=ENSTDkAm1.zip&downloadStartSecret=4jd7mmoberd",
            "ENSTDkAm2": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=ENSTDkAm2.zip&downloadStartSecret=bfeekv6zios",
            "ENSTDkCl": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=ENSTDkCl.zip&downloadStartSecret=1ekyv85ij5wh",
            "SptkBGAm": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=SptkBGAm.zip&downloadStartSecret=lckjw1lgks",
            "SptkBGCl": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=SptkBGCl.zip&downloadStartSecret=cho91vy3swp",
            "StbgTGd2": "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download?path=%2F&files=StbgTGd2.zip&downloadStartSecret=htixu8ryz3h",
        }

        self.hash_dict = {
            "AkPnBcht": "44f0b64a7cda143cfb25e217300df743",
            "AkPnBsdf": "3f26df7b2f104a3df3f27cd7b91c461a",
            "AkPnCGdD": "c15622c304e72c0d6223ce9f7070036f",
            "AkPnStgb": "75bda66fb21b927338ae2c042333175d",
            "ENSTDkAm1": "a47136282f92254722dd92aef70c6262",
            "ENSTDkAm2": "2d56f30b88e71010c66ab8c32cd7f582",
            "ENSTDkCl": "d51e503d592136f3c400f832ecb767ce",
            "SptkBGAm": "6d37344417bebd2353aeeff2b7d8232f",
            "SptkBGCl": "37d91c67d96c6612b348f171244bfb2c",
            "StbgTGd2": "2e08fc13143a525d5b316f09ed38f9d4",
        }

        self.root = root
        #         self.ext_archive = '.tar'
        self.ext_archive = ".zip"
        self.name_archive = "MAPS"
        self.original_ext = ".wav"
        self.data_type = data_type
        self.sampling_rate = sampling_rate
        self.dataset = "MAPS"

        self.groups = (
            self.available_groups(groups) if isinstance(groups, str) else list(groups)
        )

        print(f"========={self.download=}=========")
        if self.download:
            # Check if MAPS folder exists
            if not os.path.isdir(os.path.join(root, self.name_archive)):
                os.makedirs(os.path.join(root, self.name_archive))

            if self._check_all_groups_exist(
                self.groups
            ):  # If data folder does not exist, check if zip files exist
                print(f"All zip files exist.")
                self.extract_subfolders(self.groups)
                # Downsampling audio to 16kHz flac formats

        #         Downloading the complete zip file is broken at "https://amubox.univ-amu.fr/s/iNG0xc5Td1Nv4rR/download"
        #         if self.download:
        #             if os.path.isdir(os.path.join(self.root, self.name_archive)):
        #                 print(f'Dataset folder exists, skipping download...\n'
        #                       f'Checking sub-folders...')
        #                 self.extract_subfolders(groups)
        #                 self.extract_tsv()
        #             elif os.path.isfile(os.path.join(self.root, self.name_archive+self.ext_archive)):
        #                 print(f'.tar file exists, skipping download...')
        #                 print(f'Extracting MAPS.tar')
        #                 extract_archive(os.path.join(self.root, self.name_archive+self.ext_archive))
        #                 self.extract_subfolders(groups)
        #                 self.extract_tsv()
        #             else:
        #                 if not os.path.isdir(self.root):
        #                     print(f'Creating download path = {self.root}')
        #                     os.makedirs(os.path.join(self.root))

        #                 print(f'Downloading from {self.url}\n'
        #                       f"If download won't start automatically, please visit "
        #                       f"https://amubox.univ-amu.fr/index.php/s/iNG0xc5Td1Nv4rR "
        #                       f"to download manually")
        #                 download_url(self.url, root, hash_value=self.checksum, hash_type='md5')
        #                 print(f'Extracting MAPS.tar')
        #                 extract_archive(os.path.join(self.root, self.name_archive+self.ext_archive))
        #                 self.extract_subfolders(groups)
        #                 self.extract_tsv()

        else:
            if os.path.isdir(os.path.join(root, self.name_archive)):
                print(f"MAPS folder found, checking content integrity...")
                self.extract_subfolders(self.groups)
            else:
                raise ValueError(
                    f"{root} does not contain the MAPS folder, "
                    f"please specify the correct path or download it by setting `download=True`"
                )

        #         print(f"Loading {len(groups)} group{'s' if len(groups) > 1 else ''} "
        #               f"of {self.__class__.__name__} at {os.path.join(self.root, self.name_archive)}")
        self._walker = []  # if sr=none or 44100, self.walker is .wav

        for group in self.groups:
            wav_paths = glob(
                os.path.join(
                    self.root, self.name_archive, group, data_type, f"*{self.ext_audio}"
                )
            )
            self._walker.extend(wav_paths)

        if self.overlap == False:
            assert groups != "test", "When loading test set, please set overlap=True"

            #             template = pkg_resources.read_text('overlapping.pkl')
            #             with open(a, 'rb') as f:
            test_names = pickle.load(
                importlib.resources.open_binary("AudioLoader.music", "overlapping.pkl")
            )
            filtered_flacs = []
            for i in self._walker:
                if any([substring in i for substring in test_names]):
                    pass
                else:
                    filtered_flacs.append(i)
            self._walker = filtered_flacs

        if self.preload:
            self._preloader = []
            for i in tqdm(range(len(self._walker)), desc=f"Pre-loading data to RAM"):
                self._preloader.append(self.load(i))

        if self.sampling_rate and (self.sampling_rate != 44100):
            # When sampling rate is given, it will automatically create a downsampled copy
            if self.downsample_exist("flac"):
                print(f"downsampled audio exists, skipping downsampling")
            else:
                print("doing resample()")
                self.resample(sampling_rate, "flac", num_threads=4)

            for idx, audio in tqdm((enumerate(self._walker))):
                self._walker[idx] = audio.replace(".wav", ".flac")
            # if need resample, auto change .wav path to .flac path in self._walker

            # reload the flac audio after downsampling only when _walker is empty
        #             if len(self._walker) == 0:
        #                 for group in groups:
        #                     wav_paths = glob(os.path.join(self.root, self.name_archive, group, data_type, f'*{self.ext_audio}'))
        #                     self._walker.extend(wav_paths)

        print(f"{len(self._walker)} audio files found")
        if self.use_cache:
            print(
                f"use_cache={self.use_cache}: it will use existing cache files (.pt) and ignore other changes "
                f"such as ext_audio, max_midi, min_midi, and hop_length.\n"
                f"Please use .clear_caches() to remove existing .pt files to refresh caches"
            )

    def extract_subfolders(self, groups):
        missing_folder = False
        for group in groups:
            group_path = os.path.join(self.root, self.name_archive, group)
            if not os.path.isdir(group_path):
                print(f"Extracting sub-folder {group}...", end="\r")
                if group == "ENSTDkAm":
                    # ENSTDkAm consists of ENSTDkAm1.zip and ENSTDkAm2.zip
                    # Extract and merge both ENSTDkAm1.zip and ENSTDkAm2.zip as ENSTDkAm
                    check_md5(
                        os.path.join(self.root, self.name_archive, group + "1.zip"),
                        self.hash_dict[group + "1"],
                    )
                    check_md5(
                        os.path.join(self.root, self.name_archive, group + "2.zip"),
                        self.hash_dict[group + "2"],
                    )
                    extract_archive(
                        os.path.join(self.root, self.name_archive, group + "1.zip")
                    )
                    extract_archive(
                        os.path.join(self.root, self.name_archive, group + "2.zip")
                    )
                else:
                    check_md5(
                        os.path.join(self.root, self.name_archive, group + ".zip"),
                        self.hash_dict[group],
                    )
                    extract_archive(
                        os.path.join(self.root, self.name_archive, group + ".zip")
                    )
                print(f" " * 50, end="\r")
                print(f"{group} extracted.")
                missing_folder = True
        if missing_folder:
            self.extract_tsv()

    def _check_all_groups_exist(self, groups):
        print("Checking if data folders already exist...")
        for group in groups:
            if os.path.isdir(os.path.join(self.root, self.name_archive, group)):
                pass
            else:
                print(
                    f"{group} not found, proceeding to check if the zip file exists",
                    end="\r",
                )
                if (
                    group == "ENSTDkAm"
                ):  # ENSTDkAm has 2 zip files ENSTDkAm1.zip and ENSTDkAm2.zip
                    zip_list = ["ENSTDkAm1", "ENSTDkAm2"]
                    for group in zip_list:
                        self._check_and_download_zip(group)
                else:
                    self._check_and_download_zip(group)
        return True

    def _check_and_download_zip(self, group):
        if os.path.isfile(os.path.join(self.root, self.name_archive, group + ".zip")):
            print(f"{group+'.zip'} exists" + " " * 100)
            pass
        else:
            print(
                f"{os.path.isfile(os.path.join(self.root, self.name_archive, group+'.zip'))=}"
            )
            print(" " * shutil.get_terminal_size().columns, end="\r")
            print(f"{group+'.zip'} not found, proceeding to download")
            download_url(
                self.url_dict[group],
                os.path.join(self.root, self.name_archive),
                hash_value=self.hash_dict[group],
                hash_type="md5",
            )

    def extract_tsv(self):
        """
        Convert midi files into tsv files for easy loading.
        """

        tsvs = glob(
            os.path.join(self.root, self.name_archive, "*", self.data_type, "*.tsv")
        )
        num_tsvs = len(tsvs)

        if num_tsvs > 0:
            decision = input(
                f"There are already {num_tsvs} tsv files.\n"
                + f"Do you want to overwrite them? [yes/no]"
            )
        elif num_tsvs == 0:
            decision = "yes"

        if decision.lower() == "yes":
            midis = glob(
                os.path.join(self.root, self.name_archive, "*", self.data_type, "*.mid")
            )  # loading lists of midi
            Parallel(n_jobs=multiprocessing.cpu_count())(
                delayed(process_midi)(in_file, out_file)
                for in_file, out_file in files(midis, output_dir=False)
            )

    def available_groups(self, group):
        if group == "train":
            return [
                "AkPnBcht",
                "AkPnBsdf",
                "AkPnCGdD",
                "AkPnStgb",
                "SptkBGAm",
                "SptkBGCl",
                "StbgTGd2",
            ]
        elif group == "test":
            return ["ENSTDkAm", "ENSTDkCl"]
        elif group == "all":
            return [
                "AkPnBcht",
                "AkPnBsdf",
                "AkPnCGdD",
                "AkPnStgb",
                "ENSTDkAm",
                "ENSTDkCl",
                "SptkBGAm",
                "SptkBGCl",
                "StbgTGd2",
            ]

    def clear_audio(self, audio_format=".flac"):
        clear_list = []
        for group in self.groups:
            audio_paths = glob(
                os.path.join(
                    self.root,
                    self.name_archive,
                    group,
                    self.data_type,
                    f"*{audio_format}",
                )
            )
            clear_list.extend(audio_paths)

        num_files = len(clear_list)
        if num_files > 0:
            decision = input(
                f"{num_files} files found, do you want to clear them?[yes/no]"
            )
            if decision.lower() == "yes":
                for i in clear_list:
                    os.remove(i)
            elif decision.lower() == "no":
                print(f"aborting...")

    def resample(self, sr, output_format="flac", num_threads=-1):
        """
        ```python
        dataset = MAPS('./Folder', groups='all', ext_audio='.flac')
        dataset.resample(sr, output_format='flac', num_threads=4)
        ```
        It is known that sometimes num_threads>0 (using multiprocessing) might cause corrupted audio after resampling

        Resample audio clips to the target sample rate `sr` and the target format `output_format`.
        This method requires `pydub`.
        After resampling, you need to create another instance of `MAPS` in order to load the new
        audio files instead of the original `.wav` files.
        """

        from pydub import AudioSegment

        def _resample(wavfile, sr, output_format):
            sound = AudioSegment.from_wav(
                wavfile.replace(self.ext_audio, self.original_ext)
            )
            sound = sound.set_frame_rate(sr)  # downsample it to sr
            sound = sound.set_channels(1)  # Convert Stereo to Mono
            sound.export(
                wavfile.replace(self.original_ext, f".{output_format}"),
                format=output_format,
            )

        if num_threads == -1:
            Parallel(n_jobs=multiprocessing.cpu_count())(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )
        elif num_threads == 0:
            for wavfile in tqdm(
                self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
            ):
                _resample(wavfile, sr, output_format)
        else:
            Parallel(n_jobs=num_threads)(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )


class MusicNet(AMTDataset):
    def __init__(self, root="./", groups="all", sampling_rate=None, **kwargs):
        """
        root (str): The folder that contains the MusicNet dataset folder
        groups (list or str): Choose which sub-folders to load. Avaliable choices are
                              `train`, `test`, `all`. Default is `all`, which means loading
                               all sub-folders. Alternatively, users can provide a list of
                               subfolders to be loaded.
        """

        super().__init__(**kwargs)

        #         self.url = "https://homes.cs.washington.edu/~thickstn/media/musicnet.tar.gz" # old MusicNet link
        self.url = "https://zenodo.org/record/5120004/files/musicnet.tar.gz?download=1"
        self.checksum = "d41d8cd98f00b204e9800998ecf8427e"
        self.root = root
        self.ext_archive = ".tar.gz"
        self.name_archive = "musicnet"
        self.original_ext = ".wav"
        self.sampling_rate = sampling_rate
        self.dataset = "MusicNet"

        groups = groups if isinstance(groups, list) else self.available_groups(groups)
        self.groups = groups

        if self.download:
            if os.path.isdir(os.path.join(self.root, self.name_archive)):
                print(f"{self.name_archive} folder exists, skipping download")
                print(f"Converting csv files into tsv files")
                self.csv2tsv()
            elif os.path.isfile(
                os.path.join(self.root, self.name_archive + self.ext_archive)
            ):
                print(f"{self.name_archive+self.ext_archive} exists, skipping download")
                print(f"Extracting {self.name_archive+self.ext_archive}")
                extract_archive(
                    os.path.join(self.root, self.name_archive + self.ext_archive)
                )
                print(f"Converting csv files into tsv files")
                self.csv2tsv()
            else:
                if not os.path.isdir(self.root):
                    print(f"Creating download path = {self.root}")
                    os.makedirs(os.path.join(self.root))

                print(f"Downloading from {self.url}")
                download_url(self.url, root, hash_value=self.checksum, hash_type="md5")
                print(f"Extracting musicnet.tar.gz")
                extract_archive(
                    os.path.join(self.root, self.name_archive + self.ext_archive)
                )
                print(f"Converting csv files into tsv files")
                self.csv2tsv()

        else:
            if os.path.isdir(os.path.join(root, self.name_archive)):
                print(f"{self.name_archive} folder found")
            elif os.path.isfile(
                os.path.join(self.root, self.name_archive + self.ext_archive)
            ):
                print(
                    f"{self.name_archive} folder not found, but {self.name_archive+self.ext_archive} exists"
                )
                print(f"Extracting {self.name_archive+self.ext_archive}")
                extract_archive(
                    os.path.join(self.root, self.name_archive + self.ext_archive)
                )
                print(f"Converting csv files into tsv files")
                self.csv2tsv()
            else:
                raise ValueError(
                    f"{root} does not contain the MusicNet folder, "
                    f"please specify the correct path or download it by setting `download=True`"
                )

        #         print(f"Loading {len(groups)} group{'s' if len(groups) > 1 else ''} "
        #               f"of {self.__class__.__name__} at {os.path.join(self.root, self.name_archive)}")
        self._walker = []

        for group in groups:
            wav_paths = glob(
                os.path.join(
                    self.root, self.name_archive, f"{group}_data", f"*{self.ext_audio}"
                )
            )
            self._walker.extend(wav_paths)

        if self.sampling_rate and (self.sampling_rate != 44100):
            # When sampling rate is given, it will automatically create a downsampled copy
            if self.downsample_exist("flac"):
                print(f"downsampled audio exists, skipping downsampling")
            else:
                print("doing resample()")
                self.resample(sampling_rate, "flac", num_threads=4)

            for idx, audio in tqdm((enumerate(self._walker))):
                self._walker[idx] = audio.replace(".wav", ".flac")
            # if need resample, auto change .wav path to .flac path in self._walker

            # reload the flac audio after downsampling only when _walker is empty
            if len(self._walker) == 0:
                for group in groups:
                    wav_paths = glob(
                        os.path.join(
                            self.root,
                            self.name_archive,
                            f"{group}_data",
                            f"*{self.ext_audio}",
                        )
                    )
                    self._walker.extend(wav_paths)

        if self.preload:
            self._preloader = []
            for i in tqdm(range(len(self._walker)), desc=f"Pre-loading data to RAM"):
                self._preloader.append(self.load(i))

        print(f"{len(self._walker)} audio files found")

    def csv2tsv(self):
        """
        Convert csv files into tsv files for easy loading.
        """
        for group in self.groups:
            tsvs = glob(
                os.path.join(self.root, self.name_archive, f"{group}_data", "*.tsv")
            )
            num_tsvs = len(tsvs)
            if num_tsvs > 0:
                decision = input(
                    f"There are already {num_tsvs} tsv files.\n"
                    + f"Do you want to overwrite them? [yes/no]"
                )
            elif num_tsvs == 0:
                decision = "yes"

            if decision.lower() == "yes":
                csvs = glob(
                    os.path.join(
                        self.root, self.name_archive, f"{group}_labels", "*.csv"
                    )
                )  # loading lists of csvs
                Parallel(n_jobs=multiprocessing.cpu_count())(
                    delayed(process_csv)(in_file, out_file)
                    for in_file, out_file in files(csvs, output_dir=False)
                )

            # moving all tsv files to the data folder where wav files are located
            # This is to make sure we can use the same __get_item__ fuction
            tsvs = glob(
                os.path.join(self.root, self.name_archive, f"{group}_labels", "*.tsv")
            )
            for tsv in tsvs:
                target_path = tsv.replace(f"{group}_labels", f"{group}_data")
                shutil.move(tsv, target_path)

    def available_groups(self, group):
        if group == "train":
            return ["train"]
        elif group == "test":
            return ["test"]
        elif group == "all":
            return ["train", "test"]

    def resample(self, sr, output_format="flac", num_threads=-1):
        """
        ```python
        dataset = MAPS('./Folder', groups='all', ext_audio='.flac')
        dataset.resample(sr, output_format='flac', num_threads=4)
        ```
        It is known that sometimes num_threads>0 (using multiprocessing) might cause corrupted audio after resampling

        Resample audio clips to the target sample rate `sr` and the target format `output_format`.
        This method requires `pydub`.
        After resampling, you need to create another instance of `MAPS` in order to load the new
        audio files instead of the original `.wav` files.
        """

        from pydub import AudioSegment

        def _resample(wavfile, sr, output_format):
            sound = AudioSegment.from_wav(
                wavfile.replace(self.ext_audio, self.original_ext)
            )
            sound = sound.set_frame_rate(sr)  # downsample it to sr
            sound = sound.set_channels(1)  # Convert Stereo to Mono
            sound.export(
                wavfile.replace(self.original_ext, f".{output_format}"),
                format=output_format,
            )

        if num_threads == -1:
            Parallel(n_jobs=multiprocessing.cpu_count())(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )
        elif num_threads == 0:
            for wavfile in tqdm(
                self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
            ):
                _resample(wavfile, sr, output_format)
        else:
            Parallel(n_jobs=num_threads)(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )


# start of class MAESTRO
def parse_midi(path):
    """open midi file and return np.array of (onset, offset, note, velocity) rows"""
    midi = mido.MidiFile(path)

    time = 0
    sustain = False
    events = []
    for message in midi:
        time += message.time

        if (
            message.type == "control_change"
            and message.control == 64
            and (message.value >= 64) != sustain
        ):
            # sustain pedal state has just changed
            sustain = message.value >= 64
            event_type = "sustain_on" if sustain else "sustain_off"
            event = dict(
                index=len(events), time=time, type=event_type, note=None, velocity=0
            )
            events.append(event)

        if "note" in message.type:
            # MIDI offsets can be either 'note_off' events or 'note_on' with zero velocity
            velocity = message.velocity if message.type == "note_on" else 0
            event = dict(
                index=len(events),
                time=time,
                type="note",
                note=message.note,
                velocity=velocity,
                sustain=sustain,
            )
            events.append(event)

    notes = []
    for i, onset in enumerate(events):
        if onset["velocity"] == 0:
            continue

        # find the next note_off message
        offset = next(
            n for n in events[i + 1 :] if n["note"] == onset["note"] or n is events[-1]
        )

        if offset["sustain"] and offset is not events[-1]:
            # if the sustain pedal is active at offset, find when the sustain ends
            offset = next(
                n
                for n in events[offset["index"] + 1 :]
                if n["type"] == "sustain_off" or n is events[-1]
            )

        note = (onset["time"], offset["time"], onset["note"], onset["velocity"])
        notes.append(note)

    return np.array(notes)


class MAESTRO(AMTDataset):
    def __init__(self, root, groups=None, sampling_rate=None, **kwargs):
        super().__init__(**kwargs)

        self.url_dict = {
            "maestro-v2.0.0": "https://storage.googleapis.com/magentadata/datasets/maestro/v2.0.0/maestro-v2.0.0.zip",
            "maestro-v2.0.0-midi": "https://storage.googleapis.com/magentadata/datasets/maestro/v2.0.0/maestro-v2.0.0-midi.zip",
        }
        self.hash_dict = {
            "maestro-v2.0.0": "7a6c23536ebcf3f50b1f00ac253886a7",
            "maestro-v2.0.0-midi": "8a45cc678a8b23cd7bad048b1e9034c5",
        }
        self.root = root
        self.ext_archive = ".zip"
        self.name_archive = "maestro-v2.0.0"
        self.midi_name_archive = "maestro-v2.0.0-midi"
        self.original_ext = ".wav"
        self.sampling_rate = sampling_rate
        self.dataset = "MAESTRO"

        groups = groups if isinstance(groups, list) else self.available_groups(groups)
        self.groups = groups

        if self.download:
            if os.path.isdir(
                os.path.join(self.root, self.name_archive)
            ):  # check for the maestro-v2.0.0 folder
                print(f"Dataset folder exists, skipping download...")
            elif os.path.isfile(
                os.path.join(self.root, "maestro-v2.0.0.zip")
            ) and os.path.isfile(os.path.join(self.root, "maestro-v2.0.0-midi.zip")):
                # when don't have 'maestro-v2.0.0' folder, check for the zip and extract
                print(
                    f"maestro-v2.0.0.zip and maestro-v2.0.0-midi.zip exists, checking MD5..."
                )
                check_md5(
                    os.path.join(self.root, self.name_archive + self.ext_archive),
                    self.hash_dict[self.name_archive],
                )
                tqdm(
                    extract_archive(
                        os.path.join(self.root, self.name_archive + self.ext_archive)
                    )
                )

                check_md5(
                    os.path.join(self.root, self.midi_name_archive + self.ext_archive),
                    self.hash_dict[self.midi_name_archive],
                )
                tqdm(
                    extract_archive(
                        os.path.join(
                            self.root, self.midi_name_archive + self.ext_archive
                        )
                    )
                )

            elif (
                not os.path.isfile(os.path.join(self.root, "maestro-v2.0.0.zip"))
            ) and (
                not os.path.isfile(os.path.join(self.root, "maestro-v2.0.0-midi.zip"))
            ):
                # download both zip file and extract
                download_url(
                    self.url_dict["maestro-v2.0.0"],
                    self.root,
                    hash_value=self.hash_dict["maestro-v2.0.0"],
                    hash_type="md5",
                )
                download_url(
                    self.url_dict["maestro-v2.0.0-midi"],
                    self.root,
                    hash_value=self.hash_dict["maestro-v2.0.0-midi"],
                    hash_type="md5",
                )
                print(f"Download finished, extracting zip file")
                tqdm(
                    extract_archive(
                        os.path.join(self.root, self.name_archive + self.ext_archive)
                    )
                )
                tqdm(
                    extract_archive(
                        os.path.join(
                            self.root, self.midi_name_archive + self.ext_archive
                        )
                    )
                )

        elif os.path.isdir(
            os.path.join(self.root, self.name_archive)
        ):  # when download=False, and maestro-v2.0.0 folder exist
            pass
        elif os.path.isfile(
            os.path.join(self.root, "maestro-v2.0.0.zip")
        ) and os.path.isfile(os.path.join(self.root, "maestro-v2.0.0-midi.zip")):
            # when don't have 'maestro-v2.0.0' folder, check for the zip
            print(
                f"maestro-v2.0.0.zip and maestro-v2.0.0-midi.zip exists, checking MD5..."
            )
            check_md5(
                os.path.join(self.root, self.name_archive + self.ext_archive),
                self.hash_dict[self.name_archive],
            )
            extract_archive(
                os.path.join(self.root, self.name_archive + self.ext_archive)
            )

            check_md5(
                os.path.join(self.root, self.midi_name_archive + self.ext_archive),
                self.hash_dict[self.midi_name_archive],
            )
            extract_archive(
                os.path.join(self.root, self.midi_name_archive + self.ext_archive)
            )

        else:
            raise FileNotFoundError(
                f"Dataset not found at {self.root}, please specify the correct location or set `download=True`"
            )

        self._walker = (
            []
        )  # self._walker is the combined audio path audio list for all groups
        for (
            group
        ) in (
            self.groups
        ):  # self.groups is a list of str, can be more than one group inside
            self._walker.extend(self.files(group))

        if self.preload:
            self._preloader = []
            for i in tqdm(range(len(self._walker)), desc=f"Pre-loading data to RAM"):
                self._preloader.append(self.load(i))

        if self.sampling_rate and (self.sampling_rate != 44100):
            # When sampling rate is given, it will automatically create a downsampled copy
            if self.downsample_exist(
                "flac"
            ):  # it will return False if sr of existing .flac not match with self.sampling_rate
                print(f"downsampled audio exists, skipping downsampling")
            else:
                print("doing resample()")
                self.resample(self.sampling_rate, "flac", num_threads=4)

    #             # reload the flac audio after downsampling only when _walker is empty
    #             if len(self._walker) == 0:
    #                 for group in groups:
    #                     wav_paths = glob(os.path.join(self.root, self.name_archive, group, f'*{self.ext_audio}'))
    #                     self._walker.extend(wav_paths)

    def files(self, group):
        metadata = json.load(
            open(os.path.join(self.root, self.name_archive, "maestro-v2.0.0.json"))
        )
        file = sorted(
            [
                (
                    os.path.join(self.root, self.name_archive, row["audio_filename"]),
                    os.path.join(self.root, self.name_archive, row["midi_filename"]),
                )
                for row in metadata
                if row["split"] == group
            ]
        )

        files = []  # use to create [_walker]
        for audio, midi in file:
            if (self.sampling_rate is None) or (self.sampling_rate == 44100):
                files.append((audio, midi))
            else:
                self.sampling_rate != 44100
                files.append((audio.replace(".wav", ".flac"), midi))

        _walker = (
            []
        )  # _walker is the list for individaul group, _walker[idx]= ('path to wav', 'path to tsv')
        for audio_path, midi_path in tqdm(files):
            tsv_filename = midi_path.replace(".midi", ".tsv").replace(".mid", ".tsv")
            if not os.path.exists(tsv_filename):
                midi = parse_midi(midi_path)
                np.savetxt(
                    tsv_filename,
                    midi,
                    fmt="%.6f",
                    delimiter="\t",
                    header="onset,offset,note,velocity",
                )
            _walker.append((audio_path, tsv_filename))
        return _walker

    def available_groups(self, group):
        if group == "train":
            return ["train"]
        elif group == "test":
            return ["test"]
        elif group == "validation":
            return ["validation"]

    def resample(self, sr, output_format="flac", num_threads=-1):
        """
        It is known that sometimes num_threads>0 (using multiprocessing) might cause corrupted audio after resampling

        Resample audio clips to the target sample rate `sr` and the target format `output_format`.
        This method requires `pydub`.
        After resampling, you need to create another instance of `MAESTRO` in order to load the new
        audio files instead of the original `.wav` files.
        """

        from pydub import AudioSegment

        def _resample(wavfile, sr, output_format):  # wavfile is  .flac
            sound = AudioSegment.from_wav(
                wavfile.replace(".flac", self.original_ext)
            )  # wavfile now is  .wav
            sound = sound.set_frame_rate(sr)  # downsample it to sr
            sound = sound.set_channels(1)  # Convert Stereo to Mono
            sound.export(
                wavfile.replace(self.original_ext, f".{output_format}"),
                format=output_format,
            )
            # wavfile now is .flac

        if num_threads == -1:  # when num_threads==-1, it will use all the CPU
            Parallel(n_jobs=multiprocessing.cpu_count())(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile, tsvfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )

        elif num_threads == 0:
            for wavfile, tsvfile in tqdm(
                self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
            ):
                _resample(
                    wavfile, sr, output_format
                )  # wavfile is the audio path in [self._walker]
        else:
            Parallel(n_jobs=num_threads)(
                delayed(_resample)(wavfile, sr, output_format)
                for wavfile, tsvfile in tqdm(
                    self._walker, desc=f"Resampling to {sr}Hz .{output_format} files"
                )
            )
