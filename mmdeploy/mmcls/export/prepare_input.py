from typing import Any, Optional, Union

import mmcv
from mmcls.datasets import build_dataloader as build_dataloader_mmcls
from mmcls.datasets import build_dataset as build_dataset_mmcls
from mmcls.datasets.pipelines import Compose
from mmcv.parallel import collate, scatter

from mmdeploy.utils.config_utils import load_config


def create_input(model_cfg: Union[str, mmcv.Config],
                 imgs: Any,
                 device: str = 'cuda:0'):
    cfg = load_config(model_cfg)[0].copy()
    if isinstance(imgs, str):
        if cfg.data.test.pipeline[0]['type'] != 'LoadImageFromFile':
            cfg.data.test.pipeline.insert(0, dict(type='LoadImageFromFile'))
        data = dict(img_info=dict(filename=imgs), img_prefix=None)
    else:
        if cfg.data.test.pipeline[0]['type'] == 'LoadImageFromFile':
            cfg.data.test.pipeline.pop(0)
        data = dict(img=imgs)
    test_pipeline = Compose(cfg.data.test.pipeline)
    data = test_pipeline(data)
    data = collate([data], samples_per_gpu=1)
    if device != 'cpu':
        data = scatter(data, [device])[0]
    return data, data['img']


def build_dataset(dataset_cfg: Union[str, mmcv.Config],
                  dataset_type: str = 'val',
                  **kwargs):
    dataset_cfg = load_config(dataset_cfg)[0]
    data = dataset_cfg.data
    assert dataset_type in data

    dataset = build_dataset_mmcls(data[dataset_type])

    return dataset


def build_dataloader(dataset,
                     samples_per_gpu: int,
                     workers_per_gpu: int,
                     num_gpus: int = 1,
                     dist: bool = False,
                     shuffle: bool = False,
                     round_up: bool = True,
                     seed: Optional[int] = None,
                     pin_memory: bool = True,
                     persistent_workers: bool = True,
                     **kwargs):
    return build_dataloader_mmcls(dataset, samples_per_gpu, workers_per_gpu,
                                  num_gpus, dist, shuffle, round_up, seed,
                                  pin_memory, persistent_workers, **kwargs)


def get_tensor_from_input(input_data):
    return input_data['img']
