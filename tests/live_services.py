"""Opt-in provider checks, using the deployed .env and a temporary, empty profile."""
import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time


async def measure(args):
    import reports
    import tools_hub as hub
    try:
        if args.llm:
            started = time.perf_counter()
            first = None
            answer = []
            async for token in hub.stream_llm('Reply with exactly the word READY.', 'Service readiness check.'):
                if first is None:
                    first = time.perf_counter()
                answer.append(token)
            if first is None or ''.join(answer).strip().strip('.').upper() != 'READY':
                raise RuntimeError('The routed model did not finish the readiness response')
            print(f'[PASS] Live routed LLM first token {(first-started)*1000:.1f} ms; complete {(time.perf_counter()-started)*1000:.1f} ms')
        if args.video:
            video = reports.video_url(args.video)
            if not video:
                raise ValueError('Supply a public YouTube video URL')
            started = time.perf_counter()
            evidence = await reports.video_evidence(hub, video)
            if not evidence.get('text', '').strip():
                raise RuntimeError('No observations or captions returned')
            print(f"[PASS] Live video evidence via {evidence['title']} ({(time.perf_counter()-started)*1000:.1f} ms)")
        if args.notify:
            started = time.perf_counter()
            result = await hub.push_phone('Your ARGUS outbound notification test reached the messaging service.', 'ARGUS delivery test')
            if not result.get('ok'):
                raise RuntimeError('The outbound messaging server did not acknowledge the request')
            print(f'[PASS] Live ntfy server accepted the test notification ({(time.perf_counter()-started)*1000:.1f} ms)')
    finally:
        await hub.close_clients()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--llm', action='store_true', help='Make a live, potentially billable language provider request')
    parser.add_argument('--video', help='Process this public video with the configured Gemini model or public captions')
    parser.add_argument('--notify', action='store_true', help='Send a real test notification to your configured outbound topic')
    args = parser.parse_args()
    if not (args.llm or args.video or args.notify):
        parser.error('Select --llm, --video URL, or --notify')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='argus-live-check-') as temp:
        os.environ['ARGUS_DB_PATH'] = str(Path(temp)/'profile.db')
        os.environ['ARGUS_DATA_DIR'] = temp
        try:
            asyncio.run(measure(args))
        except Exception as exc:
            # Provider errors may contain private topic URLs or signed query parameters.
            print(f'[FAIL] {type(exc).__name__}. Check the selected service credentials, public video access and network connection.')
            sys.exit(1)
