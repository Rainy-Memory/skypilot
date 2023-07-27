"""InfraProvider: handles the creation and deletion of servers."""
import logging
import os
from typing import List, Dict, Set, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import requests
import pickle
import base64
import multiprocessing
import collections
import threading
import signal

import sky
from sky.backends import backend_utils

logger = logging.getLogger(__name__)

_PROCESS_POOL_REFRESH_INTERVAL = 20
# TODO(tian): Maybe let user determine this threshold
_SERVER_UNHEALTHY_THRESHOLD_SECOND = 180


class InfraProvider:
    """Each infra provider manages one services."""

    def __init__(self,
                 readiness_path: str,
                 readiness_timeout: int,
                 post_data: Optional[Any] = None) -> None:
        self.available_servers: Set[str] = set()
        self.unhealthy_servers: Set[str] = set()
        self.failed_servers: Set[str] = set()
        self.unhealthy_time: Dict[str,
                                  List[float]] = collections.defaultdict(list)
        self.readiness_path: str = readiness_path
        self.readiness_timeout: int = readiness_timeout
        self.post_data: Any = post_data
        logger.info(f'Readiness probe path: {self.readiness_path}')

    def get_replica_info(self) -> List[Dict[str, str]]:
        # Get replica info for all servers
        raise NotImplementedError

    def get_server_ips(self) -> List[str]:
        # Get all server ips
        raise NotImplementedError

    def total_server_num(self) -> int:
        # Returns the total number of servers, including those under
        # provisioning and deletion
        raise NotImplementedError

    def available_server_num(self) -> int:
        # Returns the total number of available servers
        raise NotImplementedError

    def unhealthy_server_num(self) -> int:
        # Returns the total number of unhealthy servers
        raise NotImplementedError

    def failed_server_num(self) -> int:
        # Returns the number of failed servers
        raise NotImplementedError

    def scale_up(self, n: int) -> None:
        raise NotImplementedError

    def scale_down(self, n: int) -> None:
        # TODO - Scale down must also pass in a list of servers to
        # delete or the number of servers to delete
        raise NotImplementedError

    def terminate_servers(self, unhealthy_servers: List[str]) -> None:
        # Terminates the servers with endpoints in the list
        raise NotImplementedError

    def terminate(self) -> Optional[str]:
        # Terminate service
        raise NotImplementedError

    def probe_all_endpoints(self) -> None:
        # Probe readiness of all endpoints
        raise NotImplementedError


class SkyPilotInfraProvider(InfraProvider):
    """Infra provider for SkyPilot clusters."""

    def __init__(self, task_yaml_path: str, cluster_name_prefix: str, *args,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.task_yaml_path: str = task_yaml_path
        self.cluster_name_prefix: str = cluster_name_prefix + '-'
        self.id_counter: int = 1
        self.launch_process_pool: Dict[str, multiprocessing.Process] = dict()
        self.down_process_pool: Dict[str, multiprocessing.Process] = dict()

        self._start_refresh_process_pool()

    def _refresh_process_pool(self) -> None:
        while not self.refresh_process_pool_stop_event.is_set():
            logger.info('Refreshing process pool.')
            for op, pool in zip(
                ['Launch', 'Down'],
                [self.launch_process_pool, self.down_process_pool]):
                for cluster_name, p in list(pool.items()):
                    if not p.is_alive():
                        # TODO(tian): Try-catch in process, and have an enum
                        # return value to indicate which type of failure
                        # happened.
                        logger.info(
                            f'{op} process for {cluster_name} finished.')
                        del pool[cluster_name]
                        if p.exitcode != 0:
                            logger.info(f'Process for {cluster_name} exited '
                                        f'abnormally with code {p.exitcode}.')
                            self.failed_servers.add(cluster_name)
            time.sleep(_PROCESS_POOL_REFRESH_INTERVAL)

    def _start_refresh_process_pool(self) -> None:
        self.refresh_process_pool_stop_event = threading.Event()
        self.refresh_process_pool_thread = threading.Thread(
            target=self._refresh_process_pool)
        self.refresh_process_pool_thread.start()

    def _terminate_refresh_process_pool(self) -> None:
        self.refresh_process_pool_stop_event.set()
        self.refresh_process_pool_thread.join()

    def _get_ip_clusname_map(self) -> Dict[str, str]:
        """
        Returns a map of ip to cluster name for all clusters with the prefix.
        """
        clusters = sky.global_user_state.get_clusters()
        ip_clusname_map = {}
        for cluster in clusters:
            name = cluster['name']
            if self.cluster_name_prefix in name:
                handle = cluster['handle']
                try:
                    # Get the head node ip
                    ip = backend_utils.get_node_ips(handle.cluster_yaml,
                                                    handle.launched_nodes,
                                                    handle)[0]
                    ip_clusname_map[ip] = name
                except sky.exceptions.FetchIPError:
                    logger.warning(f'Unable to get IP for cluster {name}.')
                    continue
        return ip_clusname_map

    def get_replica_info(self) -> List[Dict[str, str]]:
        clusters = sky.global_user_state.get_clusters()
        infos = []
        for cluster in clusters:
            if self.cluster_name_prefix in cluster['name']:
                info = {
                    'name': cluster['name'],
                    'handle': cluster['handle'],
                    'status': cluster['status'],
                }
                info = {
                    k: base64.b64encode(pickle.dumps(v)).decode('utf-8')
                    for k, v in info.items()
                }
                infos.append(info)
        return infos

    def get_server_ips(self) -> List[str]:
        ips = list(self._get_ip_clusname_map().keys())
        logger.info(f'Returning SkyPilot endpoints: {ips}')
        return ips

    def _return_total_servers(self) -> int:
        clusters = sky.global_user_state.get_clusters()
        # Filter out clusters that don't have the prefix
        # FIXME - this is a hack to get around.
        # should implement a better filtering mechanism
        clusters = [
            cluster for cluster in clusters
            if self.cluster_name_prefix in cluster['name']
        ]
        return len(clusters)

    def total_server_num(self) -> int:
        return self._return_total_servers()

    def available_server_num(self) -> int:
        return len(self.available_servers)

    def unhealthy_server_num(self) -> int:
        return len(self.unhealthy_servers)

    def failed_server_num(self) -> int:
        return len(self.failed_servers)

    def _launch_cluster(self, cluster_name: str, task: sky.Task) -> None:
        p = multiprocessing.Process(target=sky.launch,
                                    args=(task,),
                                    kwargs={
                                        'cluster_name': cluster_name,
                                        'detach_run': True,
                                        'retry_until_up': True
                                    })
        self.launch_process_pool[cluster_name] = p
        p.start()

    def _scale_up(self, n: int) -> None:
        # Launch n new clusters
        task = sky.Task.from_yaml(self.task_yaml_path)
        for _ in range(0, n):
            cluster_name = f'{self.cluster_name_prefix}{self.id_counter}'
            logger.info(f'Creating SkyPilot cluster {cluster_name}')
            self._launch_cluster(cluster_name, task)
            self.id_counter += 1

    def scale_up(self, n: int) -> None:
        self._scale_up(n)

    def _teardown_cluster(self, cluster_name: str) -> None:
        p = multiprocessing.Process(target=sky.down,
                                    args=(cluster_name,),
                                    kwargs={'purge': True})
        self.down_process_pool[cluster_name] = p
        p.start()

    def _scale_down(self, n: int) -> None:
        # Delete n clusters
        # Currently deletes the first n clusters
        clusters = sky.global_user_state.get_clusters()
        # Filter out clusters that don't have the prefix
        clusters = [
            cluster for cluster in clusters
            if self.cluster_name_prefix in cluster['name']
        ]
        num_clusters = len(clusters)
        if num_clusters > 0:
            if n > num_clusters:
                logger.warning(
                    f'Trying to delete {n} clusters, but only {num_clusters} '
                    'clusters exist. Deleting all clusters.')
                n = num_clusters
            for i in range(0, n):
                cluster = clusters[i]
                logger.info(f'Deleting SkyPilot cluster {cluster["name"]}')
                self._teardown_cluster(cluster['name'])

    def scale_down(self, n: int) -> None:
        self._scale_down(n)

    def terminate_servers(self, unhealthy_servers: List[str]) -> None:
        # Remove unhealthy servers from current_endpoints
        logger.info('SkyPilotInfraProvider.terminate_servers called with '
                    f'unhealthy_servers={unhealthy_servers}')
        for endpoint_url in unhealthy_servers:
            ip_to_name_map = self._get_ip_clusname_map()
            if endpoint_url not in ip_to_name_map:
                logger.warning(
                    f'Unable to find cluster name for endpoint {endpoint_url}. '
                    'Skipping.')
                continue
            name = ip_to_name_map[endpoint_url]
            if endpoint_url in unhealthy_servers:
                logger.info(f'Deleting SkyPilot cluster {name}')
                self._teardown_cluster(name)

    def terminate(self) -> Optional[str]:
        # For correctly show serve status
        self.available_servers.clear()
        self.unhealthy_servers = set(self.get_server_ips())
        self._terminate_refresh_process_pool()
        for name, p in self.launch_process_pool.items():
            # Use keyboard interrupt here since sky.launch has great
            # handling for it
            # Edge case: sky.launched finished after the
            # process_pool_refresh_process terminates
            if p.is_alive():
                assert p.pid is not None
                os.kill(p.pid, signal.SIGINT)
                p.join()
                self._teardown_cluster(name)
                logger.info(f'Interrupted launch process for cluster {name}'
                            'and deleted the cluster.')
        server_ips = self.get_server_ips()
        self.terminate_servers(server_ips)
        msg = []
        for name, p in self.down_process_pool.items():
            p.join()
            logger.info(f'Down process for cluster {name} finished.')
            if p.exitcode != 0:
                msg.append(f'Down process for cluster {name} exited abnormally'
                           f' with code {p.exitcode}. Please login to the '
                           'controller and make sure the cluster is released.')
        if not msg:
            return None
        return '\n'.join(msg)

    def probe_all_endpoints(self) -> None:
        replica_ips = set(self.get_server_ips()) - self.failed_servers

        def probe_endpoint(replica_ip: str) -> Optional[str]:
            try:
                if self.post_data:
                    response = requests.post(
                        f'http://{replica_ip}{self.readiness_path}',
                        json=self.post_data,
                        timeout=3)
                else:
                    response = requests.get(
                        f'http://{replica_ip}{self.readiness_path}', timeout=3)
                if response.status_code == 200:
                    logger.info(f'Replica {replica_ip} is available.')
                    return replica_ip
            except requests.exceptions.RequestException as e:
                logger.info(e)
                logger.info(f'Replica {replica_ip} is not available.')
                pass
            return None

        with ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(probe_endpoint, replica_ip)
                for replica_ip in replica_ips
            ]
            healthy_servers = set()
            for future in as_completed(futures):
                ip = future.result()
                if ip is not None:
                    healthy_servers.add(ip)
        logger.info(f'Healthy servers: {healthy_servers}')
        self.available_servers = healthy_servers
        unhealthy_servers = replica_ips - healthy_servers
        logger.info(f'Unhealthy servers: {unhealthy_servers}')
        self.unhealthy_servers = unhealthy_servers
        servers_to_terminate = []
        for server in unhealthy_servers:
            self.unhealthy_time[server].append(time.time())
            # coldstart time limitation is `self.readiness_timeout`.
            first_unhealthy_time = self.unhealthy_time[server][0]
            if time.time() - first_unhealthy_time > self.readiness_timeout:
                last_unhealthy_time = self.unhealthy_time[server][-1]
                if (time.time() - last_unhealthy_time >
                        _SERVER_UNHEALTHY_THRESHOLD_SECOND):
                    logger.info(f'Terminating server {server}.')
                    servers_to_terminate.append(server)
                else:
                    logger.info(f'Server {server} is unhealthy but '
                                'within unhealthy threshold. Skipping.')
            else:
                logger.info(f'Server {server} is unhealthy but within '
                            'readiness timeout. Skipping.')
        self.terminate_servers(servers_to_terminate)
