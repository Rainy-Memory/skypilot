"""LoadBalancingPolicy: Policy to select endpoint."""
import random
import typing
from typing import Dict, List, Optional, Tuple

import httpx

from sky import sky_logging

if typing.TYPE_CHECKING:
    import fastapi

logger = sky_logging.init_logger(__name__)


def _request_repr(request: 'fastapi.Request') -> str:
    return ('<Request '
            f'method="{request.method}" '
            f'url="{request.url}" '
            f'headers={dict(request.headers)} '
            f'query_params={dict(request.query_params)}>')


class LoadBalancingPolicy:
    """Abstract class for load balancing policies."""

    def __init__(self) -> None:
        self.ready_replicas: List[str] = []

    def set_ready_replicas(self, ready_replicas: List[str]) -> None:
        raise NotImplementedError

    def select_replica(self, request: 'fastapi.Request') -> Optional[str]:
        replica = self._select_replica(request)
        if replica is not None:
            logger.info(f'Selected replica {replica} '
                        f'for request {_request_repr(request)}')
        else:
            logger.warning('No replica selected for request '
                           f'{_request_repr(request)}')
        return replica

    # TODO(tian): We should have an abstract class for Request to
    # compatible with all frameworks.
    def _select_replica(self, request: 'fastapi.Request') -> Optional[str]:
        raise NotImplementedError


class RoundRobinPolicy(LoadBalancingPolicy):
    """Round-robin load balancing policy."""

    def __init__(self) -> None:
        super().__init__()
        self.index = 0

    def set_ready_replicas(self, ready_replicas: List[str]) -> None:
        if set(self.ready_replicas) == set(ready_replicas):
            return
        # If the autoscaler keeps scaling up and down the replicas,
        # we need this shuffle to not let the first replica have the
        # most of the load.
        random.shuffle(ready_replicas)
        self.ready_replicas = ready_replicas
        self.index = 0

    def _select_replica(self, request: 'fastapi.Request') -> Optional[str]:
        del request  # Unused.
        if not self.ready_replicas:
            return None
        ready_replica_url = self.ready_replicas[self.index]
        self.index = (self.index + 1) % len(self.ready_replicas)
        return ready_replica_url


class GeoDataPolicy(LoadBalancingPolicy):
    """Geo-data load balancing policy using an online GeoIP service."""

    def __init__(self, replica_locations: Dict[str, Tuple[float,
                                                          float]]) -> None:
        """
        Initialize with a mapping from replica URLs to their (latitude, longitude).
        """
        super().__init__()
        self.replica_locations = replica_locations  # type: Dict[str, Tuple[float, float]]

    def set_ready_replicas(self, ready_replicas: List[str]) -> None:
        # Ensure all replicas have associated locations
        for replica in ready_replicas:
            if replica not in self.replica_locations:
                # Every replica must have a valid location
                raise ValueError(
                    f"Replica {replica} does not have a corresponding location."
                )
        self.ready_replicas = ready_replicas

    def _select_replica(self, request: 'fastapi.Request') -> Optional[str]:
        user_location = self._get_user_location(request)
        if not user_location:
            # If user location can't be determined, select a random replica
            random.shuffle(self.ready_replicas)
            return self.ready_replicas[0]

        # Find the closest replica
        min_distance = float('inf')
        nearest_replica = None
        for replica in self.ready_replicas:
            replica_location = self.replica_locations[replica]
            distance = self._calculate_distance(user_location, replica_location)
            if distance < min_distance:
                min_distance = distance
                nearest_replica = replica

        return nearest_replica

    async def _get_user_location(
            self, request: 'fastapi.Request') -> Optional[Tuple[float, float]]:
        # Extract the user's IP address
        ip_address = request.client.host
        if not ip_address:
            logger.warning('Could not extract IP address from request.')
            return None

        # Perform GeoIP lookup using httpx, limited to 150 requests per minute.
        # TODO(acuadron): Use IP caching to reduce the number of requests.
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f'http://ip-api.com/json/{ip_address}', timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    if data['status'] == 'success':
                        latitude = data['lat']
                        longitude = data['lon']
                        return (latitude, longitude)
                    else:
                        logger.warning(
                            f"GeoIP lookup failed: {data.get('message', 'Unknown error')}"
                        )
                        return None
                else:
                    logger.warning(
                        f'GeoIP lookup failed with status code {response.status_code}'
                    )
                    return None
        except Exception as e:
            logger.warning(f'Failed to get location for IP {ip_address}: {e}')
            return None

    def _calculate_distance(self, loc1: Tuple[float, float],
                            loc2: Tuple[float, float]) -> float:
        # Haversine formula to calculate the great-circle distance
        import math
        lat1, lon1 = loc1
        lat2, lon2 = loc2
        R = 6371  # Earth radius in kilometers

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (math.sin(delta_phi / 2)**2 +
             math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        distance = R * c
        return distance
