"""The local Jaeger's query API v3, read into traces of spans: what the timings are worked out from.

Jaeger 2.21 answers in OTLP JSON. Each answer is turned into traces of spans with an id, a name, a
start and a duration in microseconds, a parent and tags, which is all the timings need. One agent's
or one run's traces are asked of Jaeger itself (`query.attributes={"slng.agent.id": ...}`), so a busy
runner's other agents never crowd the chosen one out of the trace limit. A studio run writes its
ending (`call.ending`) as a trace of its own; the traces of one run are merged into one.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

type Fetch = Callable[[str], dict[str, Any]]


class Span:
    """One OTLP span as the timings read it: Jaeger's classic shape, with ints that arrived as strings made ints."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def as_dict(self) -> dict[str, Any]:
        """The span: id, name, start and duration in microseconds, its parent, its tags, and an error status as a tag."""
        raw = self.raw
        tags = [
            {'key': attribute['key'], 'value': self.value(attribute['value'])}
            for attribute in raw.get('attributes') or []
        ]
        if (raw.get('status') or {}).get('code') in (2, 'STATUS_CODE_ERROR'):
            tags.append({'key': 'otel.status_code', 'value': 'ERROR'})
        start, end = int(raw['startTimeUnixNano']), int(raw['endTimeUnixNano'])
        parent = raw.get('parentSpanId')
        return {
            'spanID': raw['spanId'],
            'operationName': raw.get('name', ''),
            'startTime': start // 1000,
            'duration': (end - start) // 1000,
            'references': [{'refType': 'CHILD_OF', 'spanID': parent}] if parent else [],
            'tags': tags,
        }

    @classmethod
    def value(cls, value: dict[str, Any]) -> Any:
        """An OTLP attribute value as a plain one."""
        if not value:
            return None
        key, found = next(iter(value.items()))
        if key == 'intValue':
            return int(found)
        if key == 'arrayValue':
            return [cls.value(item) for item in found.get('values', [])]
        if key == 'kvlistValue':
            return {pair['key']: cls.value(pair['value']) for pair in found.get('values', [])}
        return found


class Traces:
    """Traces of spans, each `{traceID, spans}`, and the ways the timings need them grouped."""

    @staticmethod
    def of(answer: dict[str, Any]) -> list[dict[str, Any]]:
        """Jaeger API v3's OTLP JSON as traces of spans."""
        traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
        spans = (
            span
            for resource in (answer.get('result') or {}).get('resourceSpans') or []
            for scope in resource.get('scopeSpans') or []
            for span in scope.get('spans') or []
        )
        for span in spans:
            traces[span['traceId']].append(Span(span).as_dict())
        return [{'traceID': trace, 'spans': found} for trace, found in traces.items()]

    @staticmethod
    def labels(trace: dict[str, Any]) -> tuple[str, str]:
        """The run and agent a trace belongs to: the first span carrying each (every span of a studio run does)."""
        tags = [
            {tag['key']: tag.get('value') for tag in span.get('tags', [])} for span in trace.get('spans', [])
        ]
        run = next((str(tag['slng.run.id']) for tag in tags if tag.get('slng.run.id')), '')
        agent = next((str(tag['slng.agent.id']) for tag in tags if tag.get('slng.agent.id')), '')
        return run, agent

    @classmethod
    def by_run(cls, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Traces of one studio run made one, under the trace that holds the conversation; a trace naming no run stays."""
        merged: dict[str, dict[str, Any]] = {}
        kept: list[dict[str, Any]] = []
        for trace in traces:
            run, _ = cls.labels(trace)
            if not run:
                kept.append(trace)
            elif run not in merged:
                merged[run] = {'traceID': trace.get('traceID', ''), 'spans': list(trace.get('spans', []))}
                kept.append(merged[run])
            else:
                merged[run]['spans'] += trace.get('spans', [])
                if any(span.get('operationName') == 'conversation' for span in trace.get('spans', [])):
                    merged[run]['traceID'] = trace.get('traceID', '')
        return kept


class Jaeger:
    """The local Jaeger's query API: recent traces of a service, narrowed by attribute, or one trace by id."""

    UNITS: ClassVar[dict[str, int]] = {'m': 60, 'h': 3600, 'd': 86400}

    def __init__(self, url: str, fetch: Fetch | None = None, service: str = 'slng-voice-core') -> None:
        self.url = url
        self.fetch = fetch or self.fetch_json
        self.service = service

    def search(self, lookback: str, limit: int, attributes: dict[str, str]) -> list[dict[str, Any]]:
        """Recent traces of the service within `lookback` (`1h`, `24h`, `7d`), carrying these attributes."""
        start, end = self.window(lookback)
        query: dict[str, Any] = {
            'query.service_name': self.service,
            'query.start_time_min': start,
            'query.start_time_max': end,
            'query.num_traces': limit,
        }
        if attributes:
            query['query.attributes'] = json.dumps(attributes)
        return Traces.of(self.fetch(f'{self.url}/api/v3/traces?{urllib.parse.urlencode(query)}'))

    def trace(self, trace: str) -> list[dict[str, Any]]:
        """One trace by its id; empty when Jaeger has none."""
        return Traces.of(self.fetch(f'{self.url}/api/v3/traces/{urllib.parse.quote(trace)}'))

    def window(self, lookback: str) -> tuple[str, str]:
        """`1h`, `24h`, `7d` as the start and end times API v3 asks for; anything else is an hour."""
        seconds = int(lookback[:-1]) * self.UNITS[lookback[-1]] if lookback[-1:] in self.UNITS else 3600
        now = datetime.now(UTC)
        return self._stamp(now - timedelta(seconds=seconds)), self._stamp(now)

    @staticmethod
    def _stamp(moment: datetime) -> str:
        """An ISO time in UTC, as Jaeger takes it."""
        return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')

    @staticmethod
    def fetch_json(url: str) -> dict[str, Any]:
        """One API v3 answer, whole: a streamed answer is several JSON objects, merged into one `result`.

        Jaeger says "no traces found" as a 404; that is an empty answer here, not an error.
        """
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {'result': {'resourceSpans': []}}
            raise
        spans = [
            span
            for found in Jaeger._objects(text)
            for span in (found.get('result') or {}).get('resourceSpans') or []
        ]
        return {'result': {'resourceSpans': spans}}

    @staticmethod
    def _objects(text: str) -> Iterator[dict[str, Any]]:
        """Each JSON object in a text of several, written one after another."""
        decoder, at = json.JSONDecoder(), 0
        while (at := len(text) - len(text[at:].lstrip())) < len(text):
            found, at = decoder.raw_decode(text, at)
            yield found
