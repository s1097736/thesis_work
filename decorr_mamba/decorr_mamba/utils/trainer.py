from torch.utils.data import DataLoader
import math
import torch  
import torch.nn as nn
from tqdm import tqdm 
import os
import json
import wandb

from ..utils.helpers import MambaArgs, TrainingArgs
from ..model.mamba import Mamba
from ..model.decorrelation import DecorrMamba, apply_to_decorr
from ..data.synthetics import InductionData

class MambaTrainer:
	''' Trains a Mamba architecture according to a pre-specified configuration

		Args:
			mamba_args (MambaArgs): model specification
			train_args (TrainingArgs): training protocol specification
			model (Mamba): implementation of Mamba architecture as per mamba_args
			device (str): device on which to train

		Attributes:
			mamba_args (MambaArgs)
			train_args (TrainingArgs)
			model (Mamba)

		Methods:
			train(self, train_loader, val_loader, backprop): trains the architecture
				following the protocol in train_args, using provided 
				training and validation datasets
	'''
	def __init__(self, 
			mamba_args: MambaArgs, train_args: TrainingArgs, model: Mamba, device: str):

		self.mamba_args = mamba_args
		self.train_args = train_args
		self.model = model
		self.device = device

		def _add_param_to_groups(module, param_groups):
			'''
			Adds the parameters of the module to the appropriate param_groups list
			based on the presence of the _no_weight_decay attribute on the parameters.
			
			Args:
				module: a submodule of the model.
				param_groups: a dictionary containing 'decay' and 'no_decay' lists.
			'''
			for name, param in module.named_parameters(recurse=False):
				# Check if the parameter has the _no_weight_decay attribute
				if hasattr(param, '_no_weight_decay') and param._no_weight_decay:
					param_groups['no_decay'].append(param)
				else:
					param_groups['decay'].append(param)


		# collect parts of the model we don't want weight decay for. Only use weight
		# decay with AdamW optimizer
		self._param_groups = {'decay': [], 'no_decay': []}
		if self.train_args.weight_decay is not None:
			self.model.apply(lambda module: _add_param_to_groups(module, self._param_groups))
			# weight tying causes the embedding and output weights to be the same,
			# but the logic above counts this parameter twice. Remove to fix.
			del self._param_groups["decay"][-1]
	
	def train_induction(self, train_data: InductionData, val_loader: DataLoader, 
		n_epoch_steps: int, use_amp: bool, train_backprop: bool=True, train_decorr: bool=True, 
		save_checkpoints: bool=True, save_all_checkpoints: bool=False):

		''' 
		Trains the model with the protocol specified in train_args.

		Args:
			train_data (InductionData): iterator which generates the next
				training dataset batch
			n_epoch_steps (int): number of steps in an "epoch". Meaningless 
				construct since we're generating new data every time
			val_loader (DataLoader): PyTorch-compatible validation dataloader
			train_backprop (bool, optional): turns on parameter updating for everything
				other than decorrelation matrices, allows for sanity check
				of decorrelation learning rule. Defaults to 'True'
			train_decorr(bool, optional): turns on training for decorrelation matrices.
				Defaults to 'True'
			save_checkpoints (bool, optional): controls whether checkpoints are saved
				during epochs. Defaults to 'True'
			save_all_checkpoints (bool, optional): if saving checkpoints, controls
				whether all epoch checkpoints are saved or just those where the loss
				is better than the previous minimum. Defaults to 'False.	
			use_amp (bool): determines if training with automatic mixed precision 
				or not. 

		'''

		criterion = nn.CrossEntropyLoss()
		if not train_backprop:
			print("Warning: not training backpropagation parameters!")
		if not train_decorr:
			print("Warning: not training decorrelation parameters!")
		if not isinstance(self.model, DecorrMamba) and train_decorr:
			print("Warning: train_decorr set to True but model does not use decorrelation!")

		if not save_checkpoints:
			assert not save_all_checkpoints, \
			"Cannot save all checkpoints, as save_checkpoints is set to False." 

		if self.train_args.weight_decay is not None:
			optimizer = torch.optim.AdamW(
				[{'params': self._param_groups['decay'],
				  'weight_decay': self.train_args.weight_decay}, 

				 {'params': self._param_groups['no_decay'], 
				  'weight_decay': 0.0}], 

				  lr=self.train_args.lr,
				  betas=self.train_args.adam_beta,
				  eps=self.train_args.adam_epsilon)
			
		else:
			optimizer = torch.optim.Adam(self.model.parameters(), 
										lr=self.train_args.lr, 
										betas=self.train_args.adam_beta,
										eps=self.train_args.adam_epsilon)    

		if self.train_args.use_lr_sched:
			scheduler = torch.optim.lr_scheduler.LambdaLR(
				optimizer, lr_lambda=self.train_args.schedule_fn)

		min_loss = float("inf")
		
		save_path = os.path.join(".", "checkpoints")
		os.makedirs(save_path, exist_ok=True)

		scaler = torch.amp.GradScaler(self.device.type, enabled=use_amp)

		for epoch in range(self.train_args.n_epochs):
			print(f"Epoch: {epoch + 1}/{self.train_args.n_epochs}")

			self.model.train()

			assert n_epoch_steps is not None, "Specify number of steps per epoch"

			epoch_train_ce_loss = 0.0
			epoch_train_corr_loss = 0.0
			epoch_train_whit_loss = 0.0

			for i in tqdm(range(n_epoch_steps)):

				optimizer.zero_grad()
				if isinstance(self.model, DecorrMamba):
					self.model.reset_decorr()
				
				in_seq = next(train_data).to(self.device, non_blocking=True)

				assert torch.all(in_seq >= 0) and torch.all(in_seq < 16), "Data error!"

				with torch.amp.autocast(self.device.type, enabled=use_amp):
					# only care about how well the model predicts the last token
					# when seeing the cue
					out_seq = self.model(in_seq[:,:-1])
					pred = out_seq[:,-1]
					target = in_seq[:,-1]
					loss = criterion(pred, target)		

				# del in_seq
				# torch.cuda.empty_cache()

				epoch_train_ce_loss += loss.item()

				# log every 10 steps
				if i%10 == 0:
					wandb.log({"train_ce_loss": loss.item()})							
											
				if isinstance(self.model, DecorrMamba):
					# calculating mean losses across all decorrelation layers
					self.model.mean_decorr_losses()			
					train_corr_loss = self.model.mean_corr_loss.item()
					train_whit_loss = self.model.mean_whit_loss.item()
					epoch_train_corr_loss += train_corr_loss
					epoch_train_whit_loss += train_whit_loss

					if i%10 == 0:
						wandb.log({"train_corr_loss": train_corr_loss, 
								"train_whit_loss": train_whit_loss})	

				if train_backprop:
					scaler.scale(loss).backward()
				
				for name, param in self.model.named_parameters():
					if param.grad is not None:
						update_ratio = torch.norm(param.grad) / torch.norm(param)
						print(f"Layer: {name}, Update Ratio: {update_ratio.item()}")

				# gradient clipping
				if self.train_args.gradient_clip is not None:
					scaler.unscale_(optimizer)
					torch.nn.utils.clip_grad_norm_(self.model.parameters(), 
												self.train_args.gradient_clip)

				if train_backprop:
					scaler.step(optimizer)
					scaler.update()

				# update the decorrelation matrices AFTER standard backprop, 
				# else training breaks!
				if isinstance(self.model, DecorrMamba) and train_decorr:
					self.model.update_decorr_matrices()		

				if self.train_args.use_lr_sched:
					# doesn't affect decorrelation lr
					scheduler.step()

			epoch_train_ce_loss /= n_epoch_steps		
			epoch_train_corr_loss /= n_epoch_steps
			epoch_train_whit_loss /= n_epoch_steps

			print(f"Epoch train CE loss: {epoch_train_ce_loss:.4f}")
			if isinstance(self.model, DecorrMamba):			
				print(f"Epoch train correlation loss: {epoch_train_corr_loss:.4f}")
				print(f"Epoch train whitening loss: {epoch_train_whit_loss:.4f}")					

			# -------------------------------- validation -------------------------------------	

			# apply_to_decorr(self.model, lambda module: print(getattr(module, "decorr_layer")))

			self.model.eval()

			total_val_ce_loss = 0.0
			total_val_corr_loss = 0.0
			total_val_whit_loss = 0.0	

			with torch.no_grad():
				with torch.amp.autocast(self.device.type, enabled=use_amp):	
					
					for i, next_batch in enumerate(val_loader):
						in_seq = next_batch[0].to(self.device, non_blocking=True)

						assert torch.all(in_seq >= 0) and torch.all(in_seq < 16), "Data error!"

						out_seq = self.model(in_seq[:,:-1])
						pred = out_seq[:,-1]
						target = in_seq[:,-1]
						loss = criterion(pred, target)

						if loss.isnan():
							print(f"Loss in batch {i} returned nan!")
							torch.save(out_seq, os.path.join(save_path, f"error_tensor_{i}.pt"))
								
						total_val_ce_loss += loss.item()

						if isinstance(self.model, DecorrMamba):
							self.model.mean_decorr_losses()
							val_corr_loss = self.model.mean_corr_loss.item()
							val_whit_loss = self.model.mean_whit_loss.item()
							total_val_corr_loss += val_corr_loss
							total_val_whit_loss += val_whit_loss				

			total_val_ce_loss /= len(val_loader)
			print(f"Epoch val CE loss: {total_val_ce_loss:.4f}")
			wandb.log({
				"val_ce_loss": total_val_ce_loss})
			
			if isinstance(self.model, DecorrMamba):
				total_val_corr_loss /= len(val_loader)
				total_val_whit_loss /= len(val_loader)
				print(f"Epoch val correlation loss: {total_val_corr_loss:.4f}")	
				print(f"Epoch val whitening loss: {total_val_whit_loss:.4f}")				
				wandb.log({
					"val_corr_loss": total_val_corr_loss, 
					"val_whit_loss": total_val_whit_loss})

			if save_checkpoints and save_all_checkpoints:
				torch.save(
					self.model.state_dict(), os.path.join(save_path, f"epoch_{epoch}.pt"))

			if total_val_ce_loss < min_loss:
				min_loss = total_val_ce_loss
				if save_checkpoints and not save_all_checkpoints:
						torch.save(
							self.model.state_dict(), os.path.join(save_path, f"epoch_{epoch}.pt"))

		return self.model	

	def overfit_induction(self, train_data: InductionData, n_epoch_steps: int, 
		use_amp: bool, train_backprop: bool=True, train_decorr: bool=True):

		''' 
			Overfits to a single batch of training data from the induction
			dataset, to check for implementational errors

		'''

		criterion = nn.CrossEntropyLoss()

		if not train_backprop:
			print("Warning: not training backpropagation parameters!")
		if not train_decorr:
			print("Warning: not training decorrelation parameters!")
		if not isinstance(self.model, DecorrMamba) and train_decorr:
			print("Warning: train_decorr set to True but model does not use decorrelation!")

		if self.train_args.weight_decay is not None:
			optimizer = torch.optim.AdamW(
				[{'params': self._param_groups['decay'],
				  'weight_decay': self.train_args.weight_decay}, 

				 {'params': self._param_groups['no_decay'], 
				  'weight_decay': 0.0}], 

				  lr=self.train_args.lr,
				  betas=self.train_args.adam_beta,
				  eps=self.train_args.adam_epsilon)
			
		else:
			optimizer = torch.optim.Adam(self.model.parameters(), 
										lr=self.train_args.lr, 
										betas=self.train_args.adam_beta,
										eps=self.train_args.adam_epsilon)    

		if self.train_args.use_lr_sched:
			scheduler = torch.optim.lr_scheduler.LambdaLR(
				optimizer, lr_lambda=self.train_args.schedule_fn)

		
		save_path = os.path.join(".", "checkpoints")
		os.makedirs(save_path, exist_ok=True)

		scaler = torch.amp.GradScaler(self.device.type, enabled=use_amp)

		train_batch = next(train_data).to(self.device, non_blocking=True)

		for epoch in range(self.train_args.n_epochs):
			print(f"Epoch: {epoch + 1}/{self.train_args.n_epochs}")

			self.model.train()

			assert n_epoch_steps is not None, "Specify number of steps per epoch"

			epoch_train_ce_loss = 0.0
			epoch_train_corr_loss = 0.0
			epoch_train_whit_loss = 0.0

			for i in tqdm(range(n_epoch_steps)):

				optimizer.zero_grad()
				if isinstance(self.model, DecorrMamba):
					self.model.reset_decorr()

				with torch.amp.autocast(self.device.type, enabled=use_amp):
					# only care about how well the model predicts the last token
					# when seeing the cue
					out_seq = self.model(train_batch[:,:-1])
					pred = out_seq[:,-1]
					target = train_batch[:,-1]
					loss = criterion(pred, target)		

				epoch_train_ce_loss += loss.item()

				# log every 10 steps
				if i%10 == 0:
					wandb.log({"train_ce_loss": loss.item()})							
											
				if isinstance(self.model, DecorrMamba):
					# calculating mean losses across all decorrelation layers
					self.model.mean_decorr_losses()			
					train_corr_loss = self.model.mean_corr_loss.item()
					train_whit_loss = self.model.mean_whit_loss.item()
					epoch_train_corr_loss += train_corr_loss
					epoch_train_whit_loss += train_whit_loss

					if i%10 == 0:
						wandb.log({"train_corr_loss": train_corr_loss, 
								"train_whit_loss": train_whit_loss})	

				if train_backprop:
					scaler.scale(loss).backward()

				# gradient clipping
				if self.train_args.gradient_clip is not None:
					scaler.unscale_(optimizer)
					torch.nn.utils.clip_grad_norm_(self.model.parameters(), 
												self.train_args.gradient_clip)

				if train_backprop:
					scaler.step(optimizer)
					scaler.update()

				# update the decorrelation matrices AFTER standard backprop, 
				# else training breaks!
				if isinstance(self.model, DecorrMamba) and train_decorr:
					self.model.update_decorr_matrices()		

				if self.train_args.use_lr_sched:
					# doesn't affect decorrelation lr
					scheduler.step()

			epoch_train_ce_loss /= n_epoch_steps		
			epoch_train_corr_loss /= n_epoch_steps
			epoch_train_whit_loss /= n_epoch_steps

			print(f"Epoch train CE loss: {epoch_train_ce_loss:.4f}")
			if isinstance(self.model, DecorrMamba):			
				print(f"Epoch train correlation loss: {epoch_train_corr_loss:.4f}")
				print(f"Epoch train whitening loss: {epoch_train_whit_loss:.4f}")					

			torch.save(
				self.model.state_dict(), os.path.join(save_path, f"epoch_{epoch}.pt"))




