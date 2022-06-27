#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""
Training loop interception helpers
"""
import re
import torch
import pathlib
import logging
import warnings
import numpy as np
import torch.nn.functional as F
import pytorch_lightning as pl

from functools import partial
from torch.multiprocessing import Pool
from torch.optim import lr_scheduler
from typing import Callable, Dict, Optional, Sequence, Union, Any, List
from pytorch_lightning.callbacks import Callback, EarlyStopping

from kraken.lib import models, vgsl, default_specs, layers
from kraken.lib.xml import preparse_xml_data
from kraken.lib.util import make_printable
from kraken.lib.codec import PytorchCodec
from kraken.lib.dataset import (ArrowIPCRecognitionDataset,
                                GroundTruthDataset, PolygonGTDataset,
                                ImageInputTransforms, collate_sequences)
from kraken.lib.exceptions import KrakenInputException
from kraken.lib.train import _configure_optimizer_and_lr_scheduler

from torch.utils.data import DataLoader, random_split, Subset


logger = logging.getLogger(__name__)


class PretrainDataModule(pl.LightningDataModule):
    def __init__(self,
                 training_data: Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]] = None,
                 evaluation_data: Optional[Union[Sequence[Union[pathlib.Path, str]], Sequence[Dict[str, Any]]]] = None,
                 partition: Optional[float] = 0.9,
                 binary_dataset_split: bool = False,
                 batch_size: int = default_specs.RECOGNITION_PRETRAIN_HYPER_PARAMS['batch_size'],
                 num_workers: int = 1,
                 repolygonize: bool = False,
                 force_binarization: bool = False,
                 format_type: str = 'path',
                 augment: bool = default_specs.RECOGNITION_PRETRAIN_HYPER_PARAMS['augment']):
        """
        A LightningDataModule encapsulating text-less training data for
        unsupervised recognition model pretraining.

        Args:
            training_data:
            evaluation_data:
            partition:
            binary_dataset_split:
            batch_size:
            num_workers:
            force_binarization:
            format_type:
            augment:
        """
        self.save_hyperparameters()

        DatasetClass = GroundTruthDataset
        valid_norm = True
        if format_type in ['xml', 'page', 'alto']:
            logger.info(f'Parsing {len(training_data)} XML files for training data')
            training_data = preparse_xml_data(training_data, format_type, repolygonize)
            if evaluation_data:
                logger.info(f'Parsing {len(evaluation_data)} XML files for validation data')
                evaluation_data = preparse_xml_data(evaluation_data, format_type, repolygonize)
            if binary_dataset_split:
                logger.warning('Internal binary dataset splits are enabled but using non-binary dataset files. Will be ignored.')
                binary_dataset_split = False
            DatasetClass = PolygonGTDataset
            valid_norm = False
        elif format_type == 'binary':
            DatasetClass = ArrowIPCRecognitionDataset
            if repolygonize:
                logger.warning('Repolygonization enabled in `binary` mode. Will be ignored.')
            valid_norm = False
            logger.info(f'Got {len(training_data)} binary dataset files for training data')
            training_data = [{'file': file} for file in training_data]
            if evaluation_data:
                logger.info(f'Got {len(evaluation_data)} binary dataset files for validation data')
                evaluation_data = [{'file': file} for file in evaluation_data]
        elif format_type == 'path':
            if force_binarization:
                logger.warning('Forced binarization enabled in `path` mode. Will be ignored.')
                force_binarization = False
            if repolygonize:
                logger.warning('Repolygonization enabled in `path` mode. Will be ignored.')
            if binary_dataset_split:
                logger.warning('Internal binary dataset splits are enabled but using non-binary dataset files. Will be ignored.')
                binary_dataset_split = False
            logger.info(f'Got {len(training_data)} line strip images for training data')
            training_data = [{'image': im} for im in training_data]
            if evaluation_data:
                logger.info(f'Got {len(evaluation_data)} line strip images for validation data')
                evaluation_data = [{'image': im} for im in evaluation_data]
            valid_norm = True
        # format_type is None. Determine training type from length of training data entry
        elif not format_type:
            if len(training_data[0]) >= 4:
                DatasetClass = PolygonGTDataset
                valid_norm = False
            else:
                if force_binarization:
                    logger.warning('Forced binarization enabled with box lines. Will be ignored.')
                    force_binarization = False
                if repolygonize:
                    logger.warning('Repolygonization enabled with box lines. Will be ignored.')
                if binary_dataset_split:
                    logger.warning('Internal binary dataset splits are enabled but using non-binary dataset files. Will be ignored.')
                    binary_dataset_split = False
        else:
            raise ValueError(f'format_type {format_type} not in [alto, page, xml, path, binary].')

        self.transforms = ImageInputTransforms(batch,
                                               height,
                                               width,
                                               channels,
                                               self.hparams.pad,
                                               valid_norm,
                                               force_binarization)

        if evaluation_data:
            train_set = self._build_dataset(DatasetClass, training_data)
            self.train_set = Subset(train_set, range(len(train_set)))
            val_set = self._build_dataset(DatasetClass, evaluation_data)
            self.val_set = Subset(val_set, range(len(val_set)))
        elif binary_dataset_split:
            train_set = self._build_dataset(DatasetClass, training_data, split_filter='train')
            self.train_set = Subset(train_set, range(len(train_set)))
            val_set = self._build_dataset(DatasetClass, training_data, split_filter='validation')
            self.val_set = Subset(val_set, range(len(val_set)))
            logger.info(f'Found {len(self.train_set)} (train) / {len(self.val_set)} (val) samples in pre-encoded dataset')
        else:
            train_set = self._build_dataset(DatasetClass, training_data)
            train_len = int(len(train_set)*partition)
            val_len = len(train_set) - train_len
            logger.info(f'No explicit validation data provided. Splitting off '
                        f'{val_len} (of {len(train_set)}) samples to validation '
                        'set. (Will disable alphabet mismatch detection.)')
            self.train_set, self.val_set = random_split(train_set, (train_len, val_len))

        if len(self.train_set) == 0 or len(self.val_set) == 0:
            raise ValueError('No valid training data was provided to the train '
                             'command. Please add valid XML, line, or binary data.')

        logger.info(f'Training set {len(self.train_set)} lines, validation set '
                    f'{len(self.val_set)} lines, alphabet {len(train_set.alphabet)} '
                    'symbols')
        alpha_diff_only_train = set(self.train_set.dataset.alphabet).difference(set(self.val_set.dataset.alphabet))
        alpha_diff_only_val = set(self.val_set.dataset.alphabet).difference(set(self.train_set.dataset.alphabet))
        if alpha_diff_only_train:
            logger.warning(f'alphabet mismatch: chars in training set only: '
                           f'{alpha_diff_only_train} (not included in accuracy test '
                           'during training)')
        if alpha_diff_only_val:
            logger.warning(f'alphabet mismatch: chars in validation set only: {alpha_diff_only_val} (not trained)')
        logger.info('grapheme\tcount')
        for k, v in sorted(train_set.alphabet.items(), key=lambda x: x[1], reverse=True):
            char = make_printable(k)
            if char == k:
                char = '\t' + char
            logger.info(f'{char}\t{v}')

    def _build_dataset(self,
                       DatasetClass,
                       training_data,
                       **kwargs):
        dataset = DatasetClass(normalization=self.hparams.normalization,
                               whitespace_normalization=self.hparams.normalize_whitespace,
                               im_transforms=self.transforms,
                               augmentation=self.hparams.augment,
                               **kwargs)

        if (self.num_workers and self.num_workers > 1) and self.format_type != 'binary':
            with Pool(processes=self.num_workers) as pool:
                for im in pool.imap_unordered(partial(_star_fun, dataset.parse), training_data, 5):
                    logger.debug(f'Adding sample {im} to training set')
                    if im:
                        dataset.add(**im)
        else:
            for im in training_data:
                try:
                    dataset.add(**im)
                except KrakenInputException as e:
                    logger.warning(str(e))
            if self.format_type == 'binary' and self.hparams.normalization:
                logger.debug(f'Rebuilding dataset using unicode normalization')
                dataset.rebuild_alphabet()
        return dataset


class RecognitionPretrainModel(pl.LightningModule):
    def __init__(self,
                 hyper_params: Dict[str, Any] = None,
                 output: str = 'model',
                 spec: str = default_specs.RECOGNITION_SPEC,
                 model: Optional[Union[pathlib.Path, str]] = None):
        """
        A LightningModule encapsulating the unsupervised pretraining setup for
        a text recognition model.

        Setup parameters (load, training_data, evaluation_data, ....) are
        named, model hyperparameters (everything in
        `kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS`) are in in the
        `hyper_params` argument.

        Args:
            hyper_params (dict): Hyperparameter dictionary containing all fields
                                 from
                                 kraken.lib.default_specs.RECOGNITION_HYPER_PARAMS
            **kwargs: Setup parameters, i.e. CLI parameters of the train() command.
        """
        super().__init__()
        hyper_params_ = default_specs.RECOGNITION_HYPER_PARAMS
        if model:
            logger.info(f'Loading existing model from {model} ')
            self.nn = vgsl.TorchVGSLModel.load_model(model)

            if self.nn.model_type not in [None, 'recognition']:
                raise ValueError(f'Model {model} is of type {self.nn.model_type} while `recognition` is expected.')

            if load_hyper_parameters:
                hp = self.nn.hyper_params
            else:
                hp = {}
            hyper_params_.update(hp)
        else:
            self.nn = None

        if hyper_params:
            hyper_params_.update(hyper_params)
        self.save_hyperparameters(hyper_params_)

        self.model = model
        self.num_workers = num_workers
        self.format_type = format_type
        self.output = output

        self.best_epoch = 0
        self.best_metric = 0.0

        spec = spec.strip()
        if spec[0] != '[' or spec[-1] != ']':
            raise ValueError(f'VGSL spec {spec} not bracketed')
        self.spec = spec
        # preparse input sizes from vgsl string to seed ground truth data set
        # sizes and dimension ordering.
        if not self.nn:
            blocks = spec[1:-1].split(' ')
            m = re.match(r'(\d+),(\d+),(\d+),(\d+)', blocks[0])
            if not m:
                raise ValueError(f'Invalid input spec {blocks[0]}')
            batch, height, width, channels = [int(x) for x in m.groups()]
        else:
            batch, channels, height, width = self.nn.input


        if 'file_system' in torch.multiprocessing.get_all_sharing_strategies():
            logger.debug('Setting multiprocessing tensor sharing strategy to file_system')
            torch.multiprocessing.set_sharing_strategy('file_system')

        if codec:
            logger.info('Instantiating codec')
            self.codec = PytorchCodec(codec)
            for k, v in self.codec.c2l.items():
                char = make_printable(k)
                if char == k:
                    char = '\t' + char
                logger.info(f'{char}\t{v}')
        else:
            self.codec = None

        logger.info('Encoding training set')


    def forward(self, x, seq_lens):
        return self.net(x, seq_lens)

    def training_step(self, batch, batch_idx):
        # sequence batch
        if 'seq_lens' in batch:
            seq_lens, label_lens = batch['seq_lens'], batch['target_lens']
            target = (target, label_lens)
            output = self.features(batch['image'], seq_lens)
        else:
            output = self.features(batch['image'])

        mask_ouput = self.wav2vec2mask(*output)

        # run contextual encoder, i.e. recurrent layers
        output, seq_lens = self.encoder(mask_output['output'], mask_output['seq_len'])

        # height should be 1 by now
        if output.size(2) != 1:
            raise KrakenInputException('Expected dimension 3 to be 1, actual {}'.format(output.size(2)))

        y = mask_output['masked_outputs']
        x = output[..., mask_output['mask'] == 1]
        negatives = mask_output['negative_samples']

        neg_is_pos = (y == negatives).all(-1)
        y = y.unsqueeze(0)
        targets = torch.cat([y, negatives], dim=0)
        logits = torch.cosine_similarity(x.float(), targets.float(), dim=-1).type_as(x)
        logits /= self.logit_temp
        loss = F.cross_entropy(logits, target)

        return loss

    def validation_step(self, batch, batch_idx):
        pass

    def validation_epoch_end(self, outputs):
        pass

    def configure_optimizers(self):
        return _configure_optimizer_and_lr_scheduler(self.hparams,
                                                     chain(self.features.parameters(),
                                                           self.mask_layer.parameters(),
                                                           self.encoder.parameters()),
                                                     len_train_set=len(self.train_set),
                                                     loss_tracking_mode='min')


    def setup(self, stage: Optional[str] = None):
        # finalize models in case of appending/loading
        if stage in [None, 'fit']:
            if self.model:
                self.spec = self.nn.spec
            else:
                logger.info(f'Creating new model {self.spec}')
                self.nn = vgsl.TorchVGSLModel(self.spec)
                # initialize weights
                self.nn.init_weights()

            self.train_set.dataset.no_encode()
            self.val_set.dataset.no_encode()

            self.nn.hyper_params = self.hparams
            self.nn.model_type = 'recognition'

            for idx, layer in enumerate(self.nn.children()):
                if isinstance(layer, layers.TransposedSummarizingRNN):
                    break



            self.features = net.nn[:idx-1]
            self.wav2vecmask = Wav2Vec2Mask(net.nn[idx-1].output_shape,
                                            net.nn[-1].output_shape[1],
                                            self.hparams.mask_width,
                                            self.hparams.mask_prob,
                                            self.hparams.num_negatives)
            self.encoder = net.nn[idx:]

            if not self.nn.seg_type:
                logger.info(f'Setting seg_type to {self.train_set.dataset.seg_type}.')
                self.nn.seg_type = self.train_set.dataset.seg_type

            self.net = self.nn.nn

            torch.set_num_threads(max(self.num_workers, 1))

    def train_dataloader(self):
        return DataLoader(self.train_set,
                          batch_size=self.hparams.batch_size,
                          num_workers=self.num_workers,
                          pin_memory=True,
                          shuffle=True,
                          collate_fn=collate_sequences)

    def val_dataloader(self):
        return DataLoader(self.val_set,
                          shuffle=False,
                          batch_size=self.hparams.batch_size,
                          num_workers=self.num_workers,
                          pin_memory=True,
                          collate_fn=collate_sequences)

    def configure_callbacks(self):
        callbacks = []
        if self.hparams.quit == 'early':
            callbacks.append(EarlyStopping(monitor='val_accuracy',
                                           mode='max',
                                           patience=self.hparams.lag,
                                           stopping_threshold=1.0))
        return callbacks