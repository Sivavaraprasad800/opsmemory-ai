# Production Metrics Integration Example

"""
Fetch real metrics from production monitoring systems.
"""

from typing import Any
from datetime import datetime, timedelta
import requests


class ProductionMetricsAdapter:
    """Adapter to fetch metrics from production systems."""
    
    def __init__(self, metrics_source: str, api_key: str = ""):
        self.metrics_source = metrics_source
        self.api_key = api_key
    
    def fetch_metric_window(
        self,
        service_name: str,
        metric: str,
        seconds: float,
    ) -> list[tuple[datetime, float]]:
        """
        Fetch metric values over a time window.
        
        Returns: [(timestamp, value), ...]
        """
        
        if self.metrics_source == "prometheus":
            return self._fetch_from_prometheus(service_name, metric, seconds)
        
        elif self.metrics_source == "datadog":
            return self._fetch_from_datadog(service_name, metric, seconds)
        
        elif self.metrics_source == "cloudwatch":
            return self._fetch_from_cloudwatch_metrics(service_name, metric, seconds)
        
        return []
    
    def _fetch_from_prometheus(
        self, service_name: str, metric: str, seconds: float
    ) -> list[tuple[datetime, float]]:
        """Fetch from Prometheus."""
        
        # Map your metric names
        metric_map = {
            "cpu_pct": f"container_cpu_usage{{service='{service_name}'}}",
            "mem_pct": f"container_memory_usage{{service='{service_name}'}}",
            "error_rate": f"rate(http_requests_total{{service='{service_name}',status=~'5..'}}[5m])",
            "latency_ms": f"http_request_duration_milliseconds{{service='{service_name}',quantile='0.95'}}",
        }
        
        query = metric_map.get(metric, metric)
        
        response = requests.get(
            f"{self.prometheus_url}/api/v1/query_range",
            params={
                "query": query,
                "start": int((datetime.now() - timedelta(seconds=seconds)).timestamp()),
                "end": int(datetime.now().timestamp()),
                "step": "15s"
            }
        )
        
        results = []
        for result in response.json()["data"]["result"]:
            for timestamp, value in result["values"]:
                results.append((
                    datetime.fromtimestamp(timestamp),
                    float(value)
                ))
        
        return results
    
    def _fetch_from_datadog(
        self, service_name: str, metric: str, seconds: float
    ) -> list[tuple[datetime, float]]:
        """Fetch from Datadog."""
        
        # Map metric names
        metric_map = {
            "cpu_pct": f"system.cpu.user{{service:{service_name}}}",
            "mem_pct": f"system.mem.used{{service:{service_name}}}",
            "error_rate": f"trace.http.request.errors{{service:{service_name}}}.as_rate()",
        }
        
        query = metric_map.get(metric, metric)
        
        response = requests.get(
            "https://api.datadoghq.com/api/v1/query",
            params={
                "query": query,
                "from": int((datetime.now() - timedelta(seconds=seconds)).timestamp()),
                "to": int(datetime.now().timestamp()),
            },
            headers={
                "DD-API-KEY": self.api_key,
                "DD-APPLICATION-KEY": self.app_key
            }
        )
        
        results = []
        for point in response.json()["series"][0]["pointlist"]:
            timestamp, value = point
            results.append((
                datetime.fromtimestamp(timestamp / 1000),
                float(value)
            ))
        
        return results


# Usage:
# from app.integrations.production_metrics import ProductionMetricsAdapter
#
# adapter = ProductionMetricsAdapter(
#     metrics_source="prometheus",
#     api_key=settings.prometheus_api_key
# )
#
# data = adapter.fetch_metric_window(
#     service_name="payment-service",
#     metric="cpu_pct",
#     seconds=600
# )
