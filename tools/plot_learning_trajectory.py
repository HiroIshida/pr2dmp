import pickle

import tqdm
from frmax2.core import DistributionGuidedSampler

from pr2dmp.demonstration import Demonstration, DMPParameter, project_root_path

if __name__ == "__main__":
    project_name = "fridge_door_open"
    sampler_cache_dir = project_root_path(project_name) / "sampler_cache"
    idx = 0
    cache_list = []
    while True:
        try:
            full_path = sampler_cache_dir / f"cache_{idx}.pkl"
            with full_path.open(mode="rb") as f:
                cache = pickle.load(f)
            cache_list.append(cache)
            idx += 1
        except FileNotFoundError:
            break

    best_param_seq = []
    for i, cache in tqdm.tqdm(enumerate(cache_list)):
        assert isinstance(cache, DistributionGuidedSampler)
        opt_param_cache_path = sampler_cache_dir / f"opt_param_cache_{i}.pkl"
        if opt_param_cache_path.exists():
            with opt_param_cache_path.open(mode="rb") as f:
                best_param = pickle.load(f)
        else:
            best_param = cache.optimize(300)
            with opt_param_cache_path.open(mode="wb") as f:
                pickle.dump(best_param, f)
        best_param_seq.append(best_param)

    demo = Demonstration.load(project_name)
    traj_seq = []
    for param_vec in tqdm.tqdm(best_param_seq):
        param = DMPParameter()
        param.forcing_term_pos = param_vec[:30].reshape(3, 10)
        param.gripper_forcing_term = param_vec[30:].reshape(1, 10)
        traj = demo.get_dmp_trajectory(param)
        assert traj.shape == (101, 8)  # 8 for x, y, z, qx, qy, qz, qw, gripper
        traj_seq.append(traj)

    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection="3d")

    for i in tqdm.tqdm(range(len(traj_seq))):
        traj = traj_seq[i]
        if i == 0:
            ax.plot(traj[:50, 0], traj[:50, 1], traj[:50, 2], alpha=0.5, color="red")
        elif i == len(traj_seq) - 1:
            ax.plot(traj[:50, 0], traj[:50, 1], traj[:50, 2], alpha=0.5, color="blue")
        else:
            ax.plot(traj[:50, 0], traj[:50, 1], traj[:50, 2], alpha=0.5, color="gray")
    plt.show()
