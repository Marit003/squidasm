import os
from typing import Optional, Callable
from netsquid import QFormalism

from netqasm.util.yaml import dump_yaml
from netqasm.runtime.settings import Formalism
from netqasm.runtime.interface.config import default_network_config, NetworkConfig
from netqasm.sdk.config import LogConfig
from netqasm.runtime import env, process_logs
from squidasm.sim.network.nv_config import parse_nv_config, NVConfig

from squidasm.run.runtime_mgr import SquidAsmRuntimeManager
from netqasm.runtime.application import ApplicationInstance, load_yaml_file


_NS_FORMALISMS = {
    Formalism.STAB: QFormalism.STAB,
    Formalism.KET: QFormalism.KET,
    Formalism.DM: QFormalism.DM,
}


def create_nv_cfg(nv_config_file: str = None) -> NVConfig:
    if nv_config_file is None:
        nv_cfg = None
    else:
        yaml_dict = load_yaml_file(nv_config_file)
        nv_cfg = parse_nv_config(yaml_dict)
    return nv_cfg


def simulate_application(
    app_instance: ApplicationInstance,
    num_rounds: int = 1,
    network_cfg: Optional[NetworkConfig] = None,
    nv_cfg: Optional[NVConfig] = None,
    log_cfg: Optional[LogConfig] = None,
    formalism: Formalism = Formalism.KET,
    use_app_config: bool = True,
    post_function: Optional[Callable] = None,
    enable_logging: bool = True,
):
    mgr = SquidAsmRuntimeManager()
    mgr.netsquid_formalism = _NS_FORMALISMS[formalism]

    if network_cfg is None:
        node_names = [name for name in app_instance.party_alloc.keys()]
        network_cfg = default_network_config(node_names=node_names)

    mgr.set_network(cfg=network_cfg, nv_cfg=nv_cfg)

    if enable_logging:
        log_cfg = LogConfig() if log_cfg is None else log_cfg
        app_instance.logging_cfg = log_cfg

        log_dir = os.path.abspath("./log") if log_cfg.log_dir is None else log_cfg.log_dir
        if not os.path.exists(log_dir):
            os.mkdir(log_dir)

    timed_log_dir = None

    mgr.start_backend()

    for _ in range(num_rounds):
        if enable_logging:
            if log_cfg.split_runs or timed_log_dir is None:
                # create new timed directory for next run or for first run
                timed_log_dir = env.get_timed_log_dir(log_dir)

            mgr.backend_log_dir = timed_log_dir
            app_instance.logging_cfg.log_subroutines_dir = timed_log_dir
            app_instance.logging_cfg.comm_log_dir = timed_log_dir
        results = mgr.run_app(app_instance, use_app_config=use_app_config)

        if enable_logging:
            path = os.path.join(timed_log_dir, "results.yaml")
            dump_yaml(data=results, file_path=path)

    if post_function is not None:
        post_function(mgr)

    mgr.stop_backend()

    if enable_logging:
        process_logs.make_last_log(log_dir=timed_log_dir)

    return results
