class Agent:
    configs = {}  # Dict of dicts, must contain 'defaults' key.

    def __init__(self, obs_space, act_space, config):
        pass

    def init_policy(self, batch_size):
        raise NotImplementedError("init_policy(batch_size) -> state")

    def init_train(self, batch_size):
        raise NotImplementedError("init_train(batch_size) -> state")

    def policy(self, obs, state=None, mode="train"):
        raise NotImplementedError("policy(obs, state=None, mode='train') -> act, state")

    def train(self, data, state=None):
        raise NotImplementedError("train(data, state=None) -> outs, state, metrics")

    def report(self, data):
        raise NotImplementedError("report(data) -> metrics")

    def dataset(self, generator_fn):
        raise NotImplementedError("dataset(generator_fn) -> generator_fn")

    def save(self):
        raise NotImplementedError("save() -> data")

    def load(self, data):
        raise NotImplementedError("load(data) -> None")


class Env:
    def __len__(self):
        return 0  # Return positive integer for batched envs.

    def __bool__(self):
        return True  # Env is always truthy, despite length zero.

    def __repr__(self):
        return f"{self.__class__.__name__}(len={len(self)}, obs_space={self.obs_space}, act_space={self.act_space})"

    @property
    def obs_space(self):
        # The observation space must contain the keys is_first, is_last, and
        # is_terminal. Commonly, it also contains the keys reward and image. By
        # convention, keys starting with log_ are not consumed by the agent.
        raise NotImplementedError("Returns: dict of spaces")

    @property
    def act_space(self):
        # The observation space must contain the keys action and reset. This
        # restriction may be lifted in the future.
        raise NotImplementedError("Returns: dict of spaces")

    def step(self, action):
        raise NotImplementedError("Returns: dict")

    def render(self):
        raise NotImplementedError("Returns: array")

    def close(self):
        pass


class Wrapper:
    def __init__(self, env):
        self.env = env

    def __len__(self):
        return len(self.env)

    def __bool__(self):
        return bool(self.env)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return getattr(self.env, name)
        except AttributeError:
            raise ValueError(name)


class Replay:
    def __len__(self):
        raise NotImplementedError("Returns: total number of steps")

    @property
    def stats(self):
        raise NotImplementedError("Returns: metrics")

    def add(self, transition, worker=0):
        raise NotImplementedError("Returns: None")

    def add_traj(self, trajectory):
        raise NotImplementedError("Returns: None")

    def dataset(self):
        raise NotImplementedError("Yields: trajectory")

    def prioritize(self, keys, priorities):
        pass

    def save(self):
        pass

    def load(self, data):
        pass
