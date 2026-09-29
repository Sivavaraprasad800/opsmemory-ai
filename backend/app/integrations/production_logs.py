# Production Log Integration Example

"""
This module shows how to integrate real production logs.
Replace the simulator's log generation with real log fetching.
"""

from typing import Any
from datetime import datetime, timedelta
import requests


class ProductionLogAdapter:
    """Adapter to fetch logs from production systems."""
    
    def __init__(self, log_source: str, api_key: str = ""):
        self.log_source = log_source
        self.api_key = api_key
    
    def fetch_recent_logs(
        self,
        service_name: str,
        seconds: float,
        level: str | None = None,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """
        Fetch logs from production.
        
        Returns same format as simulator:
        [
            {
                "ts": "2024-12-12T14:30:00Z",
                "level": "ERROR",
                "message": "connection timeout...",
                "service_name": "payment-service"
            }
        ]
        """
        
        # EXAMPLE 1: Elasticsearch
        if self.log_source == "elasticsearch":
            return self._fetch_from_elasticsearch(service_name, seconds, level, limit)
        
        # EXAMPLE 2: CloudWatch
        elif self.log_source == "cloudwatch":
            return self._fetch_from_cloudwatch(service_name, seconds, level, limit)
        
        # EXAMPLE 3: File-based logs
        elif self.log_source == "files":
            return self._fetch_from_files(service_name, seconds, level, limit)
        
        return []
    
    def _fetch_from_elasticsearch(
        self, service_name: str, seconds: float, level: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch from Elasticsearch."""
        
        # Build query
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service": service_name}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": f"now-{int(seconds)}s"
                                }
                            }
                        }
                    ]
                }
            },
            "size": limit,
            "sort": [{"@timestamp": "desc"}]
        }
        
        if level:
            query["query"]["bool"]["must"].append(
                {"term": {"level": level.upper()}}
            )
        
        # Make request
        response = requests.post(
            f"{self.elasticsearch_url}/logs-*/_search",
            json=query,
            headers={"Authorization": f"ApiKey {self.api_key}"}
        )
        
        # Parse response
        logs = []
        for hit in response.json()["hits"]["hits"]:
            source = hit["_source"]
            logs.append({
                "ts": source.get("@timestamp"),
                "level": source.get("level", "INFO"),
                "message": source.get("message", ""),
                "service_name": service_name,
            })
        
        return logs
    
    def _fetch_from_cloudwatch(
        self, service_name: str, seconds: float, level: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch from AWS CloudWatch."""
        import boto3
        
        client = boto3.client('logs')
        
        # Calculate time range
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(seconds=seconds)).timestamp() * 1000)
        
        # Query CloudWatch
        log_group = f"/aws/service/{service_name}"
        
        query = f"fields @timestamp, level, message | filter service = '{service_name}'"
        if level:
            query += f" | filter level = '{level}'"
        query += f" | sort @timestamp desc | limit {limit}"
        
        response = client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            queryString=query
        )
        
        # Get results
        query_id = response['queryId']
        results = client.get_query_results(queryId=query_id)
        
        logs = []
        for result in results.get('results', []):
            log_dict = {field['field']: field['value'] for field in result}
            logs.append({
                "ts": log_dict.get("@timestamp"),
                "level": log_dict.get("level", "INFO"),
                "message": log_dict.get("message", ""),
                "service_name": service_name,
            })
        
        return logs
    
    def _fetch_from_files(
        self, service_name: str, seconds: float, level: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Fetch from log files."""
        import json
        from pathlib import Path
        
        log_file = Path(f"/var/log/{service_name}/application.log")
        
        logs = []
        cutoff = datetime.now() - timedelta(seconds=seconds)
        
        with open(log_file) as f:
            for line in f:
                try:
                    log = json.loads(line)
                    log_time = datetime.fromisoformat(log['timestamp'])
                    
                    if log_time < cutoff:
                        continue
                    
                    if level and log.get('level') != level:
                        continue
                    
                    logs.append({
                        "ts": log['timestamp'],
                        "level": log.get('level', 'INFO'),
                        "message": log.get('message', ''),
                        "service_name": service_name,
                    })
                    
                    if len(logs) >= limit:
                        break
                        
                except (json.JSONDecodeError, KeyError):
                    continue
        
        return logs


# Usage in your project:
# Replace simulator's recent_logs() with this:
#
# from app.integrations.production_logs import ProductionLogAdapter
#
# adapter = ProductionLogAdapter(
#     log_source="elasticsearch",
#     api_key=settings.elasticsearch_api_key
# )
#
# logs = adapter.fetch_recent_logs(
#     service_name="payment-service",
#     seconds=600
# )
