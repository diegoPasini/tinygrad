import math, time
import numpy as np
from tinygrad import Tensor, nn, GlobalCounters, TinyJit
from tinygrad.helpers import partition, trange, getenv, Context
from extra.lr_scheduler import OneCycleLR

# from https://github.com/tysam-code/hlb-CIFAR10/blob/main/main.py
batchsize = getenv("BS", 1024)
bias_scaler = 64
hyp = {
  'opt': {
    'bias_lr':        1.525 * bias_scaler/512, # TODO: Is there maybe a better way to express the bias and batchnorm scaling? :'))))
    'non_bias_lr':    1.525 / 512,
    'bias_decay':     6.687e-4 * batchsize/bias_scaler,
    'non_bias_decay': 6.687e-4 * batchsize,
    'scaling_factor': 1./9,
    'percent_start': .23,
    'loss_scale_scaler': 1./32, # * Regularizer inside the loss summing (range: ~1/512 - 16+). FP8 should help with this somewhat too, whenever it comes out. :)
  },
  'net': {
    'whitening': {
      'kernel_size': 2,
      'num_examples': 50000,
    },
    'batch_norm_momentum': .4, # * Don't forget momentum is 1 - momentum here (due to a quirk in the original paper... >:( )
    'cutmix_size': 3,
    'cutmix_epochs': 6,
    'pad_amount': 2,
    'base_depth': 64 ## This should be a factor of 8 in some way to stay tensor core friendly
  },
  'misc': {
    'ema': {
      'epochs': 10, # Slight bug in that this counts only full epochs and then additionally runs the EMA for any fractional epochs at the end too
      'decay_base': .95,
      'decay_pow': 3.,
      'every_n_steps': 5,
    },
    'train_epochs': 12.1,
    'device': 'cuda',
    'data_location': 'data.pt',
  }
}

scaler = 2. ## You can play with this on your own if you want, for the first beta I wanted to keep things simple (for now) and leave it out of the hyperparams dict
depths = {
  'init':   round(scaler**-1*hyp['net']['base_depth']), # 32  w/ scaler at base value
  'block1': round(scaler** 0*hyp['net']['base_depth']), # 64  w/ scaler at base value
  'block2': round(scaler** 2*hyp['net']['base_depth']), # 256 w/ scaler at base value
  'block3': round(scaler** 3*hyp['net']['base_depth']), # 512 w/ scaler at base value
  'num_classes': 10
}
whiten_conv_depth = 3*hyp['net']['whitening']['kernel_size']**2

class ConvGroup:
  def __init__(self, channels_in, channels_out):
    self.conv1 = nn.Conv2d(channels_in, channels_out, kernel_size=3, padding=1, bias=False)
    self.conv2 = nn.Conv2d(channels_out, channels_out, kernel_size=3, padding=1, bias=False)
    self.norm1 = nn.BatchNorm(channels_out, track_running_stats=True, eps=1e-12, momentum=hyp['net']['batch_norm_momentum'])
    self.norm2 = nn.BatchNorm(channels_out, track_running_stats=True, eps=1e-12, momentum=hyp['net']['batch_norm_momentum'])
    self.norm1.weight.requires_grad = False
    self.norm2.weight.requires_grad = False
  def __call__(self, x:Tensor) -> Tensor:
    x =    self.norm1(self.conv1(x).max_pool2d()).quick_gelu()
    return self.norm2(self.conv2(x)).quick_gelu()

class SpeedyConvNet:
  def __init__(self):
    self.whiten = nn.Conv2d(3, 2*whiten_conv_depth, kernel_size=hyp['net']['whitening']['kernel_size'], padding=0, bias=False)
    self.conv_group_1 = ConvGroup(2*whiten_conv_depth, depths['block1'])
    self.conv_group_2 = ConvGroup(depths['block1'], depths['block2'])
    self.conv_group_3 = ConvGroup(depths['block2'], depths['block3'])
    self.linear = nn.Linear(depths['block3'], depths['num_classes'], bias=False)
  def __call__(self, x:Tensor) -> Tensor:
    x = self.whiten(x).quick_gelu()
    x = x.sequential([self.conv_group_1, self.conv_group_2, self.conv_group_3])
    return self.linear(x.max(axis=(2,3))) * hyp['opt']['scaling_factor']

if __name__ == "__main__":
  # *** dataset ***
  X_train, Y_train, X_test, Y_test = nn.datasets.cifar()
  cifar10_std, cifar10_mean = X_train.float().std_mean(axis=(0, 2, 3))
  X_train = (X_train - cifar10_mean.view(1, -1, 1, 1)) / cifar10_std.view(1, -1, 1, 1)
  X_test = (X_test - cifar10_mean.view(1, -1, 1, 1)) / cifar10_std.view(1, -1, 1, 1)
  Y_train = Y_train.one_hot(depths['num_classes'])
  Y_test = Y_test.one_hot(depths['num_classes'])

  # *** model ***
  model = SpeedyConvNet()
  state_dict = nn.state.get_state_dict(model)

  #for k,v in nn.state.torch_load("/tmp/cifar_net.pt").items(): print(k)

  params_bias, params_non_bias = partition(state_dict.items(), lambda x: 'bias' in x[0])
  opt_bias     = nn.optim.SGD([x[1] for x in params_bias],     lr=0.01, momentum=.85, nesterov=True, weight_decay=hyp['opt']['bias_decay'])
  opt_non_bias = nn.optim.SGD([x[1] for x in params_non_bias], lr=0.01, momentum=.85, nesterov=True, weight_decay=hyp['opt']['non_bias_decay'])
  opt = nn.optim.OptimizerGroup(opt_bias, opt_non_bias)

  num_steps_per_epoch      = X_train.size(0) // batchsize
  total_train_steps        = math.ceil(num_steps_per_epoch * hyp['misc']['train_epochs'])
  loss_batchsize_scaler = 512/batchsize

  pct_start = hyp['opt']['percent_start']
  initial_div_factor = 1e16 # basically to make the initial lr ~0 or so :D
  final_lr_ratio = .07 # Actually pretty important, apparently!
  lr_sched_bias     = OneCycleLR(opt_bias,     max_lr=hyp['opt']['bias_lr'],     pct_start=pct_start, div_factor=initial_div_factor, final_div_factor=1./(initial_div_factor*final_lr_ratio), total_steps=total_train_steps)
  lr_sched_non_bias = OneCycleLR(opt_non_bias, max_lr=hyp['opt']['non_bias_lr'], pct_start=pct_start, div_factor=initial_div_factor, final_div_factor=1./(initial_div_factor*final_lr_ratio), total_steps=total_train_steps)

  def loss_fn(out, Y):
    return out.cross_entropy(Y, reduction='none', label_smoothing=0.2).mul(hyp['opt']['loss_scale_scaler']*loss_batchsize_scaler).sum().div(hyp['opt']['loss_scale_scaler'])

  @TinyJit
  def train_step(idxs:Tensor) -> Tensor:
    with Tensor.train():
      with Context(SPLIT_REDUCEOP=0, FUSE_ARANGE=1, NOOPT=0):
        X = X_train[idxs]
        Y = Y_train[idxs].realize(X)
      out = model(X)
      loss = loss_fn(out, Y)
      opt.zero_grad()
      loss.backward()
      opt.step()
      lr_sched_bias.step()
      lr_sched_non_bias.step()
      return loss / (batchsize*loss_batchsize_scaler)

  eval_batchsize = 2500
  @TinyJit
  def val_step() -> Tensor:
    # TODO with Tensor.no_grad()
    Tensor.no_grad = True
    loss, acc = [], []
    for i in range(0, X_test.size(0), eval_batchsize):
      out, target = model(X_test[i:i+eval_batchsize]), Y_test[i:i+eval_batchsize]
      loss.append(loss_fn(out, target))
      acc.append((out.argmax(-1).one_hot(depths['num_classes']) * target).sum() / eval_batchsize)
    ret = Tensor.stack(*loss).mean() / (batchsize*loss_batchsize_scaler), Tensor.stack(*acc).mean()
    Tensor.no_grad = False
    return ret

  np.random.seed(1337)
  for epoch in range(math.ceil(hyp['misc']['train_epochs'])):
    # TODO: move to tinygrad
    gst = time.perf_counter()
    idxs = np.arange(X_train.shape[0])
    np.random.shuffle(idxs)
    tidxs = Tensor(idxs, dtype='int')[:num_steps_per_epoch*batchsize].reshape(num_steps_per_epoch, batchsize)  # NOTE: long doesn't fold
    for epoch_step in (t:=trange(num_steps_per_epoch)):
      st = time.perf_counter()
      GlobalCounters.reset()
      loss = train_step(tidxs[epoch_step].contiguous()).item()
      t.set_description(f"loss: {loss:5.3f}   lr: {opt_non_bias.lr.item():.6f}"
                     f"   tm: {(et:=(time.perf_counter()-st))*1000:6.2f} ms {GlobalCounters.global_ops/(1e9*et):7.0f} GFLOPS")
    gmt = time.perf_counter()
    GlobalCounters.reset()
    val_loss, acc = [x.item() for x in val_step()]
    get = time.perf_counter()
    print(f"\033[F*** epoch {epoch:3d}       tm: {(gmt-gst):5.2f} s    val_time: {(get-gmt):5.2f} s   val_loss: {val_loss:5.3f}   eval acc: {acc*100:5.2f}% ***  ")
