# Author: thygate
# https://github.com/thygate/stable-diffusion-webui-depthmap-script

import modules.scripts as scripts
import gradio as gr

from modules import processing, images, shared, sd_samplers
from modules.processing import create_infotext, process_images, Processed
from modules.shared import opts, cmd_opts, state, Options
from PIL import Image

import torch
import cv2
import requests
import os.path
import contextlib

from torchvision.transforms import Compose
from repositories.midas.midas.dpt_depth import DPTDepthModel
from repositories.midas.midas.midas_net import MidasNet
from repositories.midas.midas.midas_net_custom import MidasNet_small
from repositories.midas.midas.transforms import Resize, NormalizeImage, PrepareForNet

import numpy as np
#import matplotlib.pyplot as plt

scriptname = "DepthMap v0.1.8"

class Script(scripts.Script):
	def title(self):
		return scriptname

	def show(self, is_img2img):
		return True

	def ui(self, is_img2img):

		compute_device = gr.Radio(label="Compute on", choices=['GPU','CPU'], value='GPU', type="index")
		model_type = gr.Dropdown(label="Model", choices=['dpt_large','dpt_hybrid','midas_v21','midas_v21_small'], value='dpt_large', type="index", elem_id="model_type")
		net_width = gr.Slider(minimum=64, maximum=2048, step=64, label='Net width', value=384)
		net_height = gr.Slider(minimum=64, maximum=2048, step=64, label='Net height', value=384)
		match_size = gr.Checkbox(label="Match input size",value=False)
		invert_depth = gr.Checkbox(label="Invert DepthMap (black=near, white=far)",value=False)
		save_depth = gr.Checkbox(label="Save DepthMap",value=True)
		show_depth = gr.Checkbox(label="Show DepthMap",value=True)
		combine_output = gr.Checkbox(label="Combine into one image.",value=True)
		combine_output_axis = gr.Radio(label="Combine axis", choices=['Vertical','Horizontal'], value='Horizontal', type="index")

		return [compute_device, model_type, net_width, net_height, match_size, invert_depth, save_depth, show_depth, combine_output, combine_output_axis]

	def run(self, p, compute_device, model_type, net_width, net_height, match_size, invert_depth, save_depth, show_depth, combine_output, combine_output_axis):

		def download_file(filename, url):
			print("Downloading midas model weights to %s" % filename)
			with open(filename, 'wb') as fout:
				response = requests.get(url, stream=True)
				response.raise_for_status()
				# Write response data to file
				for block in response.iter_content(4096):
					fout.write(block)

		# sd process 
		processed = processing.process_images(p)

		print('\n%s' % scriptname)
		
		# init torch device
		if compute_device == 0:
			device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
		else:
			device = torch.device("cpu")
		print("device: %s" % device)

		# model path and name
		model_dir = "./models/midas"
		# create path to model if not present
		os.makedirs(model_dir, exist_ok=True)

		print("Loading midas model weights ..")

		try:
			#"dpt_large"
			if model_type == 0: 
				model_path = f"{model_dir}/dpt_large-midas-2f21e586.pt"
				print(model_path)
				if not os.path.exists(model_path):
					download_file(model_path,"https://github.com/intel-isl/DPT/releases/download/1_0/dpt_large-midas-2f21e586.pt")
				model = DPTDepthModel(
					path=model_path,
					backbone="vitl16_384",
					non_negative=True,
				)
				net_w, net_h = 384, 384
				resize_mode = "minimal"
				normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

			#"dpt_hybrid"
			elif model_type == 1: 
				model_path = f"{model_dir}/dpt_hybrid-midas-501f0c75.pt"
				print(model_path)
				if not os.path.exists(model_path):
					download_file(model_path,"https://github.com/intel-isl/DPT/releases/download/1_0/dpt_hybrid-midas-501f0c75.pt")
				model = DPTDepthModel(
					path=model_path,
					backbone="vitb_rn50_384",
					non_negative=True,
				)
				net_w, net_h = 384, 384
				resize_mode="minimal"
				normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

			#"midas_v21"
			elif model_type == 2: 
				model_path = f"{model_dir}/midas_v21-f6b98070.pt"
				print(model_path)
				if not os.path.exists(model_path):
					download_file(model_path,"https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21-f6b98070.pt")
				model = MidasNet(model_path, non_negative=True)
				net_w, net_h = 384, 384
				resize_mode="upper_bound"
				normalization = NormalizeImage(
					mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
				)

			#"midas_v21_small"
			elif model_type == 3: 
				model_path = f"{model_dir}/midas_v21_small-70d6b9c8.pt"
				print(model_path)
				if not os.path.exists(model_path):
					download_file(model_path,"https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21_small-70d6b9c8.pt")
				model = MidasNet_small(model_path, features=64, backbone="efficientnet_lite3", exportable=True, non_negative=True, blocks={'expand': True})
				net_w, net_h = 256, 256
				resize_mode="upper_bound"
				normalization = NormalizeImage(
					mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
				)

			# override net size
			if (match_size):
				net_width, net_height = processed.width, processed.height

			# init transform
			transform = Compose(
				[
					Resize(
						net_width,
						net_height,
						resize_target=None,
						keep_aspect_ratio=True,
						ensure_multiple_of=32,
						resize_method=resize_mode,
						image_interpolation_method=cv2.INTER_CUBIC,
					),
					normalization,
					PrepareForNet(),
				]
			)

			model.eval()
			
			# optimize
			if device == torch.device("cuda"):
				model = model.to(memory_format=torch.channels_last)  
				if not cmd_opts.no_half:
					model = model.half()

			model.to(device)

			print("Computing depthmap(s) ..")
			# iterate over input (generated) images
			for count in range(0,len(processed.images)):
				# skip first (grid) image if count > 1
				if count == 0 and len(processed.images) > 1:
					continue

				# input image
				img = cv2.cvtColor(np.asarray(processed.images[count]), cv2.COLOR_BGR2RGB) / 255.0
				img_input = transform({"image": img})["image"]

				# compute
				precision_scope = torch.autocast if shared.cmd_opts.precision == "autocast" and device == torch.device("cuda") else contextlib.nullcontext
				with torch.no_grad(), precision_scope("cuda"):
					sample = torch.from_numpy(img_input).to(device).unsqueeze(0)
					if device == torch.device("cuda"):
						sample = sample.to(memory_format=torch.channels_last) 
						if not cmd_opts.no_half:
							sample = sample.half()
					prediction = model.forward(sample)
					prediction = (
						torch.nn.functional.interpolate(
							prediction.unsqueeze(1),
							size=img.shape[:2],
							mode="bicubic",
							align_corners=False,
						)
						.squeeze()
						.cpu()
						.numpy()
					)

				# output
				depth = prediction
				numbytes=2
				depth_min = depth.min()
				depth_max = depth.max()
				max_val = (2**(8*numbytes))-1

				# check output before normalizing and mapping to 16 bit
				if depth_max - depth_min > np.finfo("float").eps:
					out = max_val * (depth - depth_min) / (depth_max - depth_min)
				else:
					out = np.zeros(depth.shape)
				
				# single channel, 16 bit image
				img_output = out.astype("uint16")

				# invert depth map
				if invert_depth:
					img_output = cv2.bitwise_not(img_output)

				# three channel, 8 bits per channel image
				img_output2 = np.zeros_like(processed.images[count])
				img_output2[:,:,0] = img_output / 256.0
				img_output2[:,:,1] = img_output / 256.0
				img_output2[:,:,2] = img_output / 256.0

				# get generation parameters
				if hasattr(p, 'all_prompts') and opts.enable_pnginfo:
					info = create_infotext(p, p.all_prompts, p.all_seeds, p.all_subseeds, "", 0, 0)
				else:
					info = None

				if not combine_output:
					if show_depth:
						processed.images.append(Image.fromarray(img_output))
					if save_depth:
						# only save 16 bit single channel image when PNG format is selected
						if opts.samples_format == "png":
							images.save_image(Image.fromarray(img_output), p.outpath_samples, "", processed.seed, p.prompt, opts.samples_format, info=info, p=p, suffix="_depth")
						else:
							images.save_image(Image.fromarray(img_output2), p.outpath_samples, "", processed.seed, p.prompt, opts.samples_format, info=info, p=p, suffix="_depth")
				else:
					img_concat = np.concatenate((processed.images[count], img_output2), axis=combine_output_axis)
					if show_depth:
						processed.images.append(Image.fromarray(img_concat))
					if save_depth:
						images.save_image(Image.fromarray(img_concat), p.outpath_samples, "", processed.seed, p.prompt, opts.samples_format, info=info, p=p, suffix="_depth")

				#colormap = plt.get_cmap('inferno')
				#heatmap = (colormap(img_output2[:,:,0] / 256.0) * 2**16).astype(np.uint16)[:,:,:3]
				#processed.images.append(heatmap)
		finally:
			del model

		return processed