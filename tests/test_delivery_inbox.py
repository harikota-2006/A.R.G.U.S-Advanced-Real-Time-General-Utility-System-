"""Real temporary storage and ASGI transport; no live services or user files."""
import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx
import database
import delivery
import phone_commands
import server
import tools_hub
from auth import AuthError
from session_manager import ArgusSession


def artifact_fixture():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('ppt/presentation.xml', '<presentation/>')
    raw = stream.getvalue()
    return {'artifact': {'filename': 'ECE notes.pptx', 'media_type': delivery.PPTX_MIME,
        'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'data_base64': base64.b64encode(raw).decode()},
        'opened': True, 'path': 'C:/Documents/ECE notes.pptx'}


class StorageFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = {'ARGUS_SESSION_SECRET': 'test-session-secret-longer-than-32-characters',
            'ARGUS_PUBLIC_URL': 'https://argus.example', 'NTFY_COMMAND_KEY': 'test-phone-command-key-longer-than-32-characters'}
        for module, attribute, value in ((database, 'DB_PATH', self.root / 'memory.db'),
                (database, 'DATA_DIR', self.root), (delivery, 'DATA_DIR', self.root),
                (tools_hub, 'DATA_DIR', self.root),
                (delivery, 'env', lambda key, default='': self.settings.get(key, default)),
                (phone_commands, 'env', lambda key, default='': self.settings.get(key, default))):
            handle = patch.object(module, attribute, value)
            handle.start()
            self.addCleanup(handle.stop)
        database._bootstrap_schema()
        self.original_connections = set(server.connections)
        server.connections.clear()
        self.addCleanup(self.restore_connections)

    def restore_connections(self):
        server.connections.clear()
        server.connections.update(self.original_connections)


class DeliveryTests(StorageFixture):
    async def test_export_roundtrip_and_scoped_http_download(self):
        source = artifact_fixture()
        result = await delivery.publish_export(source)
        self.assertNotIn('artifact', result)
        path, metadata = delivery.export_file(result['id'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), metadata['sha256'])
        query = parse_qs(urlsplit(result['download_url']).query)
        self.assertTrue(delivery.valid_share(result['id'], query['expires'][0], query['signature'][0]))
        self.assertFalse(delivery.valid_share('0' * 32, query['expires'][0], query['signature'][0]))
        transport = httpx.ASGITransport(app=server.app)
        with patch.object(server.auth, 'verify_token', side_effect=AuthError('Locked')):
            async with httpx.AsyncClient(transport=transport, base_url='https://argus.example') as client:
                denied = await client.get(result['download_path'])
                shared = await client.get(result['download_url'])
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(shared.status_code, 200)
                self.assertEqual(shared.content, path.read_bytes())
                self.assertIn('attachment;', shared.headers['content-disposition'])
                self.assertEqual(shared.headers['cache-control'], 'no-store')
        with patch.object(delivery.time, 'time', return_value=int(query['expires'][0]) + 1):
            self.assertFalse(delivery.valid_share(result['id'], query['expires'][0], query['signature'][0]))

    async def test_export_rejects_tampering_before_writing(self):
        for field, value in (('sha256', '0' * 64), ('size', 999), ('data_base64', '!bad'), ('media_type', 'text/plain')):
            source = artifact_fixture()
            source['artifact'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                await delivery.publish_export(source)
        self.assertFalse((self.root / 'exports').exists())
        with self.assertRaises(FileNotFoundError):
            delivery.export_file('../.env')

    async def test_local_exports_work_without_public_url(self):
        self.settings['ARGUS_PUBLIC_URL'] = ''
        result = await delivery.publish_export(artifact_fixture())
        self.assertEqual(result['download_url'], '')
        self.assertTrue(result['url'].startswith('/api/exports/'))

    async def test_bad_public_url_does_not_leave_orphan_files(self):
        self.settings['ARGUS_PUBLIC_URL'] = 'https://user:password@example.com'
        with self.assertRaises(ValueError):
            await delivery.publish_export(artifact_fixture())
        self.assertFalse((self.root / 'exports').exists())


class InboxTests(StorageFixture):
    def connection(self):
        connection = types.SimpleNamespace(session=ArgusSession(authenticated=True), token='fixture', closed=False,
            send=AsyncMock(), start_text=AsyncMock(), queue_announcement=MagicMock())
        # SimpleNamespace isn't hashable; a minimal concrete connection is.
        value = type('FakeConnection', (), {})()
        value.__dict__.update(connection.__dict__)
        server.connections.add(value)
        return value

    async def test_pdf_attachment_ingestion_idempotence_and_study_receipt(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        writer.add_metadata({'/Title': 'ECE syllabus'})
        buffer = io.BytesIO()
        writer.write(buffer)
        connection = self.connection()
        event = {'id': 'pdf-one', 'event': 'message', 'attachment': {'url': 'https://ntfy.sh/file/test.pdf', 'name': 'syllabus.pdf', 'type': 'application/pdf'}}
        download = AsyncMock(return_value=(buffer.getvalue(), 'application/pdf', 'syllabus.pdf'))
        with patch.object(tools_hub, 'download_attachment', download):
            first = await server.process_inbox_event(event)
            second = await server.process_inbox_event(event)
        self.assertEqual(first['status'], 'indexed')
        self.assertEqual(second['status'], 'duplicate')
        download.assert_awaited_once()
        self.assertEqual(connection.session.mode, 'study')
        self.assertTrue(Path(connection.session.last_pdf['original_path']).is_file())
        self.assertEqual(database.db.study_coverage([first['document_id']])['total'], 2)
        notifications = [call.args[0] for call in connection.send.call_args_list]
        self.assertTrue(any('exactly 2 pages' in event.get('text', '') for event in notifications))
        self.assertTrue(any(event['type'] == 'study_progress' for event in notifications))
        self.assertIn('exactly 2 pages', connection.queue_announcement.call_args.args[0])

    async def test_direct_pdf_link_uses_existing_host_checked_downloader(self):
        document = database.db.store_pdf('notes.pdf', ['Voltage is potential difference.'])
        download = AsyncMock(return_value=(b'%PDF fixture', 'application/pdf', 'notes.pdf'))
        with patch.object(tools_hub, 'download_attachment', download), patch.object(tools_hub, 'ingest_study_material', AsyncMock(return_value=document)):
            result = await server.process_inbox_event({'id': 'link', 'message': 'Study this https://files.example/notes.pdf?token=fixture'})
        self.assertEqual(result['status'], 'indexed')
        self.assertEqual(download.call_args.args[0]['url'], 'https://files.example/notes.pdf?token=fixture')

    async def test_failed_pdf_is_retried_three_times_not_silently_dropped(self):
        event = {'id': 'bad-pdf', 'attachment': {'name': 'bad.pdf', 'url': 'https://ntfy.sh/bad.pdf'}}
        download = AsyncMock(side_effect=RuntimeError('network failure'))
        with patch.object(tools_hub, 'download_attachment', download):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await server.process_inbox_event(event)
            self.assertEqual((await server.process_inbox_event(event))['status'], 'failed')
            self.assertEqual((await server.process_inbox_event(event))['status'], 'duplicate')
        self.assertEqual(download.await_count, 3)
        self.assertEqual(database.db.get_latest_inbox()['id'], 'bad-pdf')

    async def test_signed_command_needs_unlocked_session_and_nonce_is_single_use(self):
        envelope = phone_commands.sign_command('detective mode', self.settings['NTFY_COMMAND_KEY'])
        event = {'id': 'command-1', 'message': json.dumps(envelope)}
        self.assertEqual((await server.process_inbox_event(event))['status'], 'locked')
        connection = self.connection()
        with patch.object(server.auth, 'verify_token', return_value={'exp': time.time() + 60}):
            event['id'] = 'command-2'
            self.assertEqual((await server.process_inbox_event(event))['status'], 'dispatched')
            event['id'] = 'command-3'
            with self.assertRaises(ValueError):
                await server.process_inbox_event(event)
        connection.start_text.assert_awaited_once_with('detective mode', active=True)

    async def test_unsigned_notes_never_execute(self):
        connection = self.connection()
        result = await server.process_inbox_event({'id': 'note', 'message': 'Open Paint and send my files'})
        self.assertEqual(result['status'], 'stored')
        connection.start_text.assert_not_awaited()

    async def test_signed_arbitrary_action_and_remote_consent_are_denied(self):
        connection = self.connection()
        for command in ('open a terminal and run a script', 'yes'):
            envelope = phone_commands.sign_command(command, self.settings['NTFY_COMMAND_KEY'])
            receipt = await server.process_inbox_event({'id': command, 'message': json.dumps(envelope)})
            self.assertEqual(receipt['status'], 'denied')
        connection.start_text.assert_not_awaited()

    async def test_inbox_bearer_is_scoped_to_exact_origin(self):
        requests = []
        def receive(request):
            requests.append(request)
            return httpx.Response(200, content=b'fixture', headers={'Content-Type':'text/plain'})
        self.settings.update({'NTFY_RX_URL':'https://inbox.example:8443/in',
                              'NTFY_TOKEN':'fixture-token','NTFY_ATTACHMENT_HOSTS':'files.example'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            with patch.object(tools_hub, 'client', return_value=client), patch.object(tools_hub, 'env', lambda key, default='': self.settings.get(key, default)):
                for url in ('https://inbox.example:8443/file','https://files.example/file','https://inbox.example/file'):
                    await tools_hub.download_attachment({'url':url,'name':'note.txt'})
                with self.assertRaises(tools_hub.ArgusToolError):
                    await tools_hub.download_attachment({'url':'https://inbox.example:9000/file'})
        self.assertEqual([r.headers.get('authorization') for r in requests],['Bearer fixture-token',None,None])

    async def test_phone_envelope_tamper_and_expiry(self):
        key = self.settings['NTFY_COMMAND_KEY']
        envelope = phone_commands.sign_command('study mode', key)
        self.assertEqual(phone_commands.verify_command(json.dumps(envelope))[1], 'study mode')
        envelope['command'] = 'open Paint'
        with self.assertRaisesRegex(ValueError, 'signature'):
            phone_commands.verify_command(json.dumps(envelope))
        expired = phone_commands.sign_command('study mode', key, timestamp=int(time.time()) - 600)
        with self.assertRaisesRegex(ValueError, 'Expired'):
            phone_commands.verify_command(json.dumps(expired))


if __name__ == '__main__':
    unittest.main()
