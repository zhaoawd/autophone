"""Persist each external query response before interpretation or report rendering."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json

from autophone.observation.telemetry import TelemetryReplayUnavailable
from autophone.observation.data_trace import BehaviorEvent, DataTraceBatch


class CollectionQueries:
    def __init__(self, journal):
        self.journal = journal

    def __call__(self, name, request, invoke):
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
        row = self.journal.read()
        checkpoints = row.get('collection_checkpoints') or {}
        start, done = f'{name}_started', f'{name}_complete'
        if done in checkpoints:
            saved = checkpoints[done]
            if saved['request_hash'] != fingerprint:
                raise TelemetryReplayUnavailable('frozen query changed')
            if saved['kind'] == 'data-trace-batch':
                values = dict(saved['response'])
                events = []
                for item in values['events']:
                    event = dict(item)
                    for field in ('occurred_at', 'ingested_at'):
                        event[field] = datetime.fromisoformat(event[field]) if event[field] else None
                    events.append(BehaviorEvent(**event))
                values['events'] = tuple(events)
                values['queried_at'] = datetime.fromisoformat(values['queried_at'])
                return DataTraceBatch(**values)
            return saved['response']
        if start in checkpoints:
            raise TelemetryReplayUnavailable('query response unavailable after interruption; refusing to query again')
        self.journal.checkpoint(start, {'request_hash': fingerprint}, expected_version=row['version'])
        response = invoke()
        kind = 'data-trace-batch' if isinstance(response, DataTraceBatch) else 'json'
        encoded = asdict(response) if kind == 'data-trace-batch' else response
        encoded = json.loads(json.dumps(encoded, default=lambda value: value.isoformat()))
        row = self.journal.read()
        self.journal.checkpoint(done, {'request_hash': fingerprint, 'kind': kind, 'response': encoded},
                                expected_version=row['version'])
        return response
