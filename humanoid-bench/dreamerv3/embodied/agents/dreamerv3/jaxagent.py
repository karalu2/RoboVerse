import functools
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial as bind

import chex
import embodied
import jax
import jax.numpy as jnp
import numpy as np
from jax._src import checkify as src
from jax.experimental import checkify

from . import jaxutils
from . import ninjax as nj

tree_map = jax.tree_util.tree_map
tree_leaves = jax.tree_util.tree_leaves


def patched(self):
    return f"nan generated by primitive: {self.prim} at:\n\n" + f"{self.traceback_info}"


src.NaNError.__str__ = patched


def patched():
    return str(src.source_info_util.summarize(src.source_info_util.current(), 30))


src.summary = patched


def Wrapper(agent_cls):
    class Agent(JAXAgent):
        configs = agent_cls.configs
        inner = agent_cls

        def __init__(self, *args, **kwargs):
            super().__init__(agent_cls, *args, **kwargs)

    return Agent


class JAXAgent(embodied.Agent):
    def __init__(self, agent_cls, obs_space, act_space, config):
        print("Observation space")
        [print(f"  {k:<16} {v}") for k, v in obs_space.items()]
        print("Action space")
        [print(f"  {k:<16} {v}") for k, v in act_space.items()]

        self.obs_space = obs_space
        self.act_space = act_space
        self.config = config
        self.jaxcfg = config.jax
        self.logdir = embodied.Path(config.logdir)
        self._setup()
        self.agent = agent_cls(obs_space, act_space, config, name="agent")
        self.rng = np.random.default_rng(config.seed)
        self.keys = [
            k for k in list(obs_space.keys()) + list(act_space.keys()) if not k.startswith("_") and k != "reset"
        ]

        self.train_static = {}
        self.policy_static = {"mode": "train"}

        available = jax.devices(self.jaxcfg.platform)
        print(f"JAX devices ({jax.local_device_count()}):", available)
        if self.jaxcfg.assert_num_devices > 0:
            assert len(available) == self.jaxcfg.assert_num_devices, (
                available,
                len(available),
                self.jaxcfg.assert_num_devices,
            )
        self.policy_devices = [available[i] for i in self.jaxcfg.policy_devices]
        self.train_devices = [available[i] for i in self.jaxcfg.train_devices]
        print("Policy devices:", ", ".join([str(x) for x in self.policy_devices]))
        print("Train devices: ", ", ".join([str(x) for x in self.train_devices]))
        self.single_device = (self.policy_devices == self.train_devices) and (len(self.policy_devices) == 1)

        self.outs_worker = ThreadPoolExecutor(1, "jaxagent_outs")
        self.mets_worker = ThreadPoolExecutor(1, "jaxagent_mets")
        self.sync_worker = ThreadPoolExecutor(1, "jaxagent_sync")
        self.outs_promise = None
        self.mets_promise = None
        self.sync_promise = None

        self._transform()
        self.params_lock = threading.Lock()
        with embodied.timer.section("agent_init"):
            self.params = self._init_varibs(obs_space, act_space)
        self.updates = embodied.Counter()

        pattern = re.compile(self.jaxcfg.policy_keys)
        self.policy_keys = [k for k in self.params.keys() if pattern.search(k)]
        self.should_sync = embodied.when.Every(self.jaxcfg.sync_every)
        self.prev_params = {k: self.params[k] for k in self.policy_keys}
        if not self.single_device:
            self.policy_params = self._copy_params(self.prev_params)

    def init_policy(self, batch_size):
        varibs = self.prev_params if self.single_device else self.policy_params
        rng = self._next_rngs(self.policy_devices)
        is_first = np.ones(batch_size, bool)
        is_first = self._convert_inps(is_first, self.policy_devices)
        state, _ = self._init_policy(varibs, rng, is_first)
        state = self._convert_outs(state, self.policy_devices)
        return state

    def init_train(self, batch_size):
        rng = self._next_rngs(self.train_devices)
        is_first = np.ones((batch_size, self.config.batch_length), bool)
        is_first = self._convert_inps(is_first, self.train_devices)
        state, _ = self._init_train(self.params, rng, is_first)
        return state

    @embodied.timer.section("jaxagent_policy")
    def policy(self, obs, state, **kwargs):
        assert state is not None
        # if state is None:
        #   state = self.init_policy(len(obs['is_first']))
        obs = self._filter_data(obs)

        with embodied.timer.section("prepare_state"):
            state = tree_map(
                lambda x: np.asarray(x) if isinstance(x, list) else x,
                state,
                is_leaf=lambda x: isinstance(x, list),
            )
        with embodied.timer.section("upload_inputs"):
            obs, state = self._convert_inps((obs, state), self.policy_devices)
            rng = self._next_rngs(self.policy_devices)

        kwargs = {**self.policy_static, **kwargs}
        with embodied.timer.section("agent_policy"):
            varibs = self.prev_params if self.single_device else self.policy_params
            (outs, state), _ = self._policy(varibs, rng=rng, obs=obs, carry=state, **kwargs)

        if not self.single_device:
            with embodied.timer.section("swap_varibs"):
                if self.sync_promise and self.sync_promise.done():
                    self.policy_params = self.sync_promise.result()
                    self.sync_promise = None
        with embodied.timer.section("fetch_outputs"):
            outs, state = self._convert_outs((outs, state), self.policy_devices)
        return outs, state

    @embodied.timer.section("jaxagent_train")
    def train(self, data, state, **kwargs):
        assert state is not None
        data = data.copy()
        rng = data.pop("rng")
        data = self._filter_data(data)
        kwargs = {**self.train_static, **kwargs}

        self.prev_params = {k: self.params[k] for k in self.policy_keys}
        with embodied.timer.section("agent_train"):
            items = self.params.items()
            allocated = {k: v for k, v in items if k in self.policy_keys}
            donated = {k: v for k, v in items if k not in self.policy_keys}
            with self.params_lock:
                (outs, state, mets), self.params = self._train(
                    allocated,
                    donated=donated,
                    rng=rng,
                    data=data,
                    carry=state,
                    **kwargs,
                )

        self.updates.increment()

        if not self.single_device:
            if not self.sync_promise and self.should_sync(self.updates):
                self.sync_promise = self.sync_worker.submit(self._copy_params, self.prev_params, block=True)

        return_outs = {}
        if self.outs_promise:
            return_outs = self.outs_promise.result()
        self.outs_promise = self.outs_worker.submit(self._convert_outs, outs, self.train_devices)

        return_mets = {}
        if self.mets_promise and self.mets_promise.done():
            return_mets = self.mets_promise.result()
            self.mets_promise = None
        if not self.mets_promise:
            # Only request metrics if we aren't currently waiting for previous
            # metrics. This means we'll skip the metrics of some training steps if
            # fetching them from device would slow down the training loop.
            self.mets_promise = self.mets_worker.submit(self._convert_mets, mets, self.train_devices)

        if self.jaxcfg.profiler:
            outdir, copyto = self.logdir, None
            if str(outdir).startswith(("gs://", "/gcs/")):
                copyto = outdir
                outdir = embodied.Path("/tmp/profiler")
                outdir.mkdirs()
            if self.updates == 100:
                embodied.print(f"Start JAX profiler: {outdir!s}", "yellow")
                jax.profiler.start_trace(str(outdir))
            if self.updates == 140:
                from embodied.core import path as pathlib

                embodied.print("Stop JAX profiler", "yellow")
                jax.profiler.stop_trace()
                if copyto:
                    pathlib.GFilePath(outdir).copy(copyto)
                    print(f"Copied profiler result {outdir} to {copyto}")

        return return_outs, state, return_mets

    @embodied.timer.section("jaxagent_report")
    def report(self, data):
        data = data.copy()
        rng = data.pop("rng")
        data = self._filter_data(data)
        with embodied.timer.section("agent_report"):
            mets, _ = self._report(self.params, rng, data)
        mets = self._convert_mets(mets, self.train_devices)
        return mets

    def dataset(self, generator):
        def transform(data):
            return {
                **self._convert_inps(data, self.train_devices),
                "rng": self._next_rngs(self.train_devices),
            }

        return embodied.Prefetch(generator, transform)

    @embodied.timer.section("jaxagent_save")
    def save(self):
        with self.params_lock:
            if len(self.train_devices) > 1:
                varibs = tree_map(lambda x: x[0], self.params)
            else:
                varibs = self.params
            return jax.device_get(varibs)

    @embodied.timer.section("jaxagent_load")
    def load(self, state):
        with self.params_lock:
            del self.prev_params
            if len(self.train_devices) == 1:
                chex.assert_trees_all_equal_shapes(self.params, state)
                del self.params
                self.params = jax.device_put(state, self.train_devices[0])
            else:
                chex.assert_trees_all_equal_shapes(tree_map(lambda x: x[0], self.params), state)
                del self.params
                self.params = jax.device_put_replicated(state, self.train_devices)
            self.prev_params = {k: self.params[k] for k in self.policy_keys}
            if not self.single_device:
                self.policy_params = self._copy_params(self.prev_params)

    def _setup(self):
        try:
            import tensorflow as tf

            tf.config.set_visible_devices([], "GPU")
            tf.config.set_visible_devices([], "TPU")
        except Exception as e:
            print("Could not disable TensorFlow devices:", e)
        if not self.jaxcfg.prealloc:
            os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
        os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "0.8"
        xla_flags = []
        if self.jaxcfg.logical_cpus:
            count = self.jaxcfg.logical_cpus
            xla_flags.append(f"--xla_force_host_platform_device_count={count}")
        if xla_flags:
            os.environ["XLA_FLAGS"] = " ".join(xla_flags)
        jax.config.update("jax_platform_name", self.jaxcfg.platform)
        jax.config.update("jax_disable_jit", not self.jaxcfg.jit)
        # jax.config.update('jax_debug_nans', self.jaxcfg.debug_nans)
        if self.jaxcfg.transfer_guard:
            jax.config.update("jax_transfer_guard", "disallow")
        if self.jaxcfg.platform == "cpu":
            jax.config.update("jax_disable_most_optimizations", self.jaxcfg.debug)
        jaxutils.COMPUTE_DTYPE = getattr(jnp, self.jaxcfg.compute_dtype)
        jaxutils.PARAM_DTYPE = getattr(jnp, self.jaxcfg.param_dtype)

    def _transform(self):
        self._init_train = nj.pure(lambda x: self.agent.train_initial(len(x)))
        self._init_policy = nj.pure(lambda x: self.agent.policy_initial(len(x)))
        self._train = nj.pure(self.agent.train)
        self._policy = nj.pure(self.agent.policy)
        self._report = nj.pure(self.agent.report)

        self._init_policy = bind(self._init_policy, modify=False)
        self._train = lambda state, donated, rng, data, fn=self._train, **kw: (
            fn({**state, **donated}, rng=rng, data=data, **kw)
        )
        self._policy = bind(self._policy, modify=False)
        self._report = bind(self._report, modify=False)

        train_static = list(self.train_static.keys()) + ["create", "modify"]
        policy_static = list(self.policy_static.keys()) + ["create", "modify"]
        train_donate = ["donated", "rng", "data", "carry"]
        policy_donate = ["rng", "obs", "carry"]

        if len(self.train_devices) == 1:
            kw = dict(device=self.train_devices[0])
            self._init_train = nj.jit(self._init_train, **kw)
            self._train = nj.jit(self._train, static=train_static, donate=train_donate, **kw)
            self._report = nj.jit(self._report, **kw)
        else:
            kw = dict(devices=self.train_devices)
            self._init_train = nj.pmap(self._init_train, "i", **kw)
            self._train = nj.pmap(self._train, "i", static=train_static, donate=train_donate, **kw)
            self._report = nj.pmap(self._report, "i", **kw)

        if len(self.policy_devices) == 1:
            kw = dict(device=self.policy_devices[0])
            self._init_policy = nj.jit(self._init_policy, **kw)
            self._policy = nj.jit(self._policy, static=policy_static, donate=policy_donate, **kw)
        else:
            kw = dict(devices=self.policy_devices)
            self._init_policy = nj.pmap(self._init_policy, "i", **kw)
            self._policy = nj.pmap(self._policy, "i", static=policy_static, donate=policy_donate, **kw)

        self._init_policy = bind(self._init_policy, init=False)
        self._policy = bind(self._policy, init=False)
        self._report = bind(self._report, init=False)

        if self.jaxcfg.checks:
            assert self.config.run.actor_threads == 1, "chex is not thread-safe"
            jaxutils.ENABLE_CHECKS = True
            wrap = nj.static_support(self._checkify)
            self._policy = wrap(self._policy, static=policy_static)
            self._train = wrap(self._train, static=train_static + ["init"])
            self._report = wrap(self._report)
            self._init_train = wrap(self._init_train)
            self._init_policy = wrap(self._init_policy)

    def _checkify(self, fun):
        fun = chex.chexify(fun, True, checkify.user_checks)

        @functools.wraps(fun)
        def transformed(*args, **kwargs):
            try:
                return fun(*args, **kwargs)
            except Exception:
                chex.block_until_chexify_assertions_complete()
                raise

        return transformed

    def _convert_inps(self, value, devices, rng=False, block=False):
        if len(devices) == 1:
            value = jax.device_put(value, devices[0])
        else:
            D = len(devices)
            try:
                value = tree_map(lambda x: x.reshape((D, -1, *x.shape[1:])), value)
            except Exception as e:
                shapes = tree_map(lambda x: x.shape, value)
                msg = f"Shapes are not divislbe by {D} devices: {shapes}"
                raise ValueError(msg) from e
            shards = [tree_map(lambda x: x[d], value) for d in range(D)]
            value = jax.device_put_sharded(shards, devices)
        if rng:
            value["rng"] = self._next_rngs(devices)
        if block:
            jax.block_until_ready(value)
        return value

    def _convert_outs(self, value, devices):
        value = jax.device_get(value)
        if len(devices) > 1:
            value = tree_map(lambda x: x.reshape((-1,) + x.shape[2:]), value)
        return value

    def _convert_mets(self, value, devices):
        if len(devices) > 1:
            value = tree_map(lambda x: x[0], value)
        return jax.device_get(value)

    def _next_rngs(self, devices, mirror=False):
        high = np.iinfo(np.uint32).max
        if len(devices) == 1:
            return jax.device_put(self.rng.integers(0, high, (2,), np.uint32), devices[0])
        elif mirror:
            return jax.device_put_replicated(self.rng.integers(0, high, (2,), np.uint32), devices)
        else:
            return jax.device_put_sharded(list(self.rng.integers(0, high, (len(devices), 2), np.uint32)), devices)

    def _init_varibs(self, obs_space, act_space):
        varibs = {}
        rng = self._next_rngs(self.train_devices, mirror=True)
        dims = (self.config.batch_size, self.config.batch_length)
        data = self._dummy_batch({**obs_space, **act_space}, dims)
        data = self._convert_inps(data, self.train_devices)
        state, varibs = self._init_train(varibs, rng, data["is_first"])
        varibs = self._train(varibs, rng=rng, data=data, carry=state, donated={}, apply=False)
        # obs = self._dummy_batch(obs_space, (1,))
        # state, varibs = self._init_policy(varibs, rng, obs['is_first'])
        # varibs = self._policy(
        #     varibs, rng, obs, state, mode='train', init_only=True)
        # print('Params:', embodied.format(varibs))
        return varibs

    @embodied.timer.section("copy_varibs")
    def _copy_params(self, varibs, block=False):
        if self.single_device:
            return varibs
        if len(self.train_devices) > 1:
            varibs = tree_map(lambda x: x[0].device_buffer, varibs)
        if len(self.policy_devices) == 1:
            varibs = jax.device_put(varibs, self.policy_devices[0])
        else:
            varibs = jax.device_put_replicated(varibs, self.policy_devices)
        if block:
            jax.block_until_ready(varibs)
        return varibs

    def _filter_data(self, data):
        return {k: v for k, v in data.items() if k in self.keys}

    def _dummy_batch(self, spaces, batch_dims):
        spaces = [(k, v) for k, v in spaces.items()]
        data = {k: np.zeros(v.shape, v.dtype) for k, v in spaces}
        data = self._filter_data(data)
        for dim in reversed(batch_dims):
            data = {k: np.repeat(v[None], dim, axis=0) for k, v in data.items()}
        return data
