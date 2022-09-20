# Copyright (c) OpenMMLab. All rights reserved.
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import mmcv
import mmengine
import numpy as np
import torch
import torch.nn as nn
from mmcv.parallel import collate, scatter
from mmdet3d.core.bbox import get_box_type
from mmdet3d.datasets.pipelines import Compose
from torch.utils.data import DataLoader, Dataset

from mmdeploy.codebase.base import BaseTask
from mmdeploy.codebase.mmdet3d.deploy.mmdetection3d import MMDET3D_TASK
from mmdeploy.utils import Task, get_root_logger, load_config
from .voxel_detection_model import VoxelDetectionModel


@MMDET3D_TASK.register_module(Task.VOXEL_DETECTION.value)
class VoxelDetection(BaseTask):

    def __init__(self, model_cfg: mmengine.Config, deploy_cfg: mmengine.Config,
                 device: str):
        super().__init__(model_cfg, deploy_cfg, device)

    def build_backend_model(self,
                            model_files: Sequence[str] = None,
                            **kwargs) -> torch.nn.Module:
        """Initialize backend model.

        Args:
            model_files (Sequence[str]): Input model files.

        Returns:
            nn.Module: An initialized backend model.
        """
        from .voxel_detection_model import build_voxel_detection_model
        model = build_voxel_detection_model(
            model_files, self.model_cfg, self.deploy_cfg, device=self.device)
        return model

    def build_pytorch_model(self,
                            model_checkpoint: Optional[str] = None,
                            cfg_options: Optional[Dict] = None,
                            **kwargs) -> torch.nn.Module:
        """Initialize torch model.

        Args:
            model_checkpoint (str): The checkpoint file of torch model,
                defaults to `None`.
            cfg_options (dict): Optional config key-pair parameters.
        Returns:
            nn.Module: An initialized torch model generated by other OpenMMLab
                codebases.
        """
        from mmdet3d.apis import init_model
        device = self.device
        model = init_model(self.model_cfg, model_checkpoint, device)
        return model.eval()

    def create_input(self, pcd: str, *args) -> Tuple[Dict, torch.Tensor]:
        """Create input for detector.

        Args:
            pcd (str): Input pcd file path.

        Returns:
            tuple: (data, input), meta information for the input pcd
                and model input.
        """
        data = VoxelDetection.read_pcd_file(pcd, self.model_cfg, self.device)
        voxels, num_points, coors = VoxelDetectionModel.voxelize(
            self.model_cfg, data['points'][0])
        return data, (voxels, num_points, coors)

    def visualize(self,
                  model: torch.nn.Module,
                  image: str,
                  result: list,
                  output_file: str,
                  window_name: str,
                  show_result: bool = False,
                  score_thr: float = 0.3):
        """Visualize predictions of a model.

        Args:
            model (nn.Module): Input model.
            image (str): Pcd file to draw predictions on.
            result (list): A list of predictions.
            output_file (str): Output file to save result.
            window_name (str): The name of visualization window. Defaults to
                an empty string.
            show_result (bool): Whether to show result in windows, defaults
                to `False`.
            score_thr (float): The score threshold to display the bbox.
                Defaults to 0.3.
        """
        from mmdet3d.apis import show_result_meshlab
        data = VoxelDetection.read_pcd_file(image, self.model_cfg, self.device)
        show_result_meshlab(
            data,
            result,
            output_file,
            score_thr,
            show=show_result,
            snapshot=1 - show_result,
            task='det')

    @staticmethod
    def read_pcd_file(pcd: str, model_cfg: Union[str, mmengine.Config],
                      device: str) -> Dict:
        """Read data from pcd file and run test pipeline.

        Args:
            pcd (str): Pcd file path.
            model_cfg (str | mmengine.Config): The model config.
            device (str): A string specifying device type.

        Returns:
            dict: meta information for the input pcd.
        """
        if isinstance(pcd, (list, tuple)):
            pcd = pcd[0]
        model_cfg = load_config(model_cfg)[0]
        test_pipeline = Compose(model_cfg.data.test.pipeline)
        box_type_3d, box_mode_3d = get_box_type(
            model_cfg.data.test.box_type_3d)
        data = dict(
            pts_filename=pcd,
            box_type_3d=box_type_3d,
            box_mode_3d=box_mode_3d,
            # for ScanNet demo we need axis_align_matrix
            ann_info=dict(axis_align_matrix=np.eye(4)),
            sweeps=[],
            # set timestamp = 0
            timestamp=[0],
            img_fields=[],
            bbox3d_fields=[],
            pts_mask_fields=[],
            pts_seg_fields=[],
            bbox_fields=[],
            mask_fields=[],
            seg_fields=[])
        data = test_pipeline(data)
        data = collate([data], samples_per_gpu=1)
        data['img_metas'] = [
            img_metas.data[0] for img_metas in data['img_metas']
        ]
        data['points'] = [point.data[0] for point in data['points']]
        if device != 'cpu':
            data = scatter(data, [device])[0]
        return data

    @staticmethod
    def run_inference(model: nn.Module,
                      model_inputs: Dict[str, torch.Tensor]) -> List:
        """Run inference once for a object detection model of mmdet3d.

        Args:
            model (nn.Module): Input model.
            model_inputs (dict): A dict containing model inputs tensor and
                meta info.

        Returns:
            list: The predictions of model inference.
        """
        result = model(
            return_loss=False,
            points=model_inputs['points'],
            img_metas=model_inputs['img_metas'])
        return [result]

    @staticmethod
    def evaluate_outputs(model_cfg,
                         outputs: Sequence,
                         dataset: Dataset,
                         metrics: Optional[str] = None,
                         out: Optional[str] = None,
                         metric_options: Optional[dict] = None,
                         format_only: bool = False,
                         log_file: Optional[str] = None):
        if out:
            logger = get_root_logger()
            logger.info(f'\nwriting results to {out}')
            mmcv.dump(outputs, out)
        kwargs = {} if metric_options is None else metric_options
        if format_only:
            dataset.format_results(outputs, **kwargs)
        if metrics:
            eval_kwargs = model_cfg.get('evaluation', {}).copy()
            # hard-code way to remove EvalHook args
            for key in [
                    'interval', 'tmpdir', 'start', 'gpu_collect', 'save_best',
                    'rule'
            ]:
                eval_kwargs.pop(key, None)
                eval_kwargs.pop(key, None)
            eval_kwargs.update(dict(metric=metrics, **kwargs))
            dataset.evaluate(outputs, **eval_kwargs)

    def get_model_name(self, *args, **kwargs) -> str:
        """Get the model name.

        Return:
            str: the name of the model.
        """
        raise NotImplementedError

    def get_tensor_from_input(self, input_data: Dict[str, Any],
                              **kwargs) -> torch.Tensor:
        """Get input tensor from input data.

        Args:
            input_data (dict): Input data containing meta info and image
                tensor.
        Returns:
            torch.Tensor: An image in `Tensor`.
        """
        raise NotImplementedError

    def get_partition_cfg(partition_type: str, **kwargs) -> Dict:
        """Get a certain partition config for mmdet.

        Args:
            partition_type (str): A string specifying partition type.

        Returns:
            dict: A dictionary of partition config.
        """
        raise NotImplementedError

    def get_postprocess(self, *args, **kwargs) -> Dict:
        """Get the postprocess information for SDK.

        Return:
            dict: Composed of the postprocess information.
        """
        raise NotImplementedError

    def get_preprocess(self, *args, **kwargs) -> Dict:
        """Get the preprocess information for SDK.

        Return:
            dict: Composed of the preprocess information.
        """
        raise NotImplementedError

    def single_gpu_test(self,
                        model: nn.Module,
                        data_loader: DataLoader,
                        show: bool = False,
                        out_dir: Optional[str] = None,
                        **kwargs) -> List:
        """Run test with single gpu.

        Args:
            model (nn.Module): Input model from nn.Module.
            data_loader (DataLoader): PyTorch data loader.
            show (bool): Specifying whether to show plotted results. Defaults
                to `False`.
            out_dir (str): A directory to save results, defaults to `None`.

        Returns:
            list: The prediction results.
        """
        model.eval()
        results = []
        dataset = data_loader.dataset

        prog_bar = mmcv.ProgressBar(len(dataset))
        for i, data in enumerate(data_loader):
            with torch.no_grad():
                result = model(data['points'][0].data,
                               data['img_metas'][0].data, False)
            if show:
                # Visualize the results of MMDetection3D model
                # 'show_results' is MMdetection3D visualization API
                if out_dir is None:
                    model.module.show_result(
                        data,
                        result,
                        out_dir='',
                        file_name='',
                        show=show,
                        snapshot=False,
                        score_thr=0.3)
                else:
                    model.module.show_result(
                        data,
                        result,
                        out_dir=out_dir,
                        file_name=f'model_output{i}',
                        show=show,
                        snapshot=True,
                        score_thr=0.3)
            results.extend(result)

            batch_size = len(result)
            for _ in range(batch_size):
                prog_bar.update()
        return results
