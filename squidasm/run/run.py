from time import sleep
from multiprocessing.pool import ThreadPool

from netqasm.sdk.shared_memory import reset_memories
from netqasm.logging import get_netqasm_logger
from squidasm.backend import Backend

logger = get_netqasm_logger()


def as_completed(futures):
    futures = list(futures)
    while len(futures) > 0:
        for i, future in enumerate(futures):
            if future.ready():
                futures.pop(i)
                yield future
        sleep(0.1)


def run_applications(applications, post_function=None, instr_log_dir=None, network_config=None):
    """Executes functions containing application scripts,

    Parameters
    ----------
    applications : dict
        Keys should be names of nodes
        Values should be the functions
    post_function : None or function
        A function to be applied to the backend (:class:`~.backend.Backend`)
        after the execution. This can be used for debugging, e.g. getting the
        quantum states after execution etc.
    """
    reset_memories()
    node_names = list(applications.keys())
    apps = list(applications.values())

    def run_backend():
        logger.debug(f"Starting netsquid backend thread with nodes {node_names}")
        backend = Backend(node_names, instr_log_dir=instr_log_dir, network_config=network_config)
        backend.start()
        if post_function is not None:
            post_function(backend)
        logger.debug("End backend thread")

    with ThreadPool(len(node_names) + 1) as executor:
        # Start the backend thread
        backend_future = executor.apply_async(run_backend)

        # Start the application threads
        app_futures = []
        for app in apps:
            if isinstance(app, tuple):
                app_func, kwargs = app
                future = executor.apply_async(app_func, kwds=kwargs)
            else:
                future = executor.apply_async(app)
            app_futures.append(future)

        # Join the application threads and the backend
        for future in as_completed([backend_future] + app_futures):
            future.get()

    reset_memories()
