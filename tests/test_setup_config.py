"""Exercise configuration migration with disposable fixture keys."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dotenv import dotenv_values

import setup_config


class SetupConfigTests(unittest.TestCase):
    def test_tablet_merge_keeps_pin_hash_profile_and_existing_node_key(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base/'.env.example').write_bytes((setup_config.BASE/'.env.example').read_bytes())
            (base/'.env').write_text('GROQ_API_KEY=\nARGUS_DB_PATH=/private/profile.db\n')
            legacy = base/'legacy.env'
            legacy.write_text('ARGUS_PIN_HASH=test-existing-hash\nARGUS_WORKSTATION_TOKEN='+'x'*32+'\nGROQ_API_KEY=test-provider-key\n')
            with patch.object(setup_config,'BASE',base), patch('sys.argv',['setup','--legacy-env',str(legacy)]), patch('builtins.input',return_value=''), patch('getpass.getpass',side_effect=AssertionError('Replaced existing PIN')), redirect_stdout(io.StringIO()):
                setup_config.main()
            config = dotenv_values(base/'.env',interpolate=False)
            self.assertEqual(config['ARGUS_PIN_HASH'],'test-existing-hash')
            self.assertEqual(config['ARGUS_DB_PATH'],'/private/profile.db')
            self.assertEqual(config['ARGUS_DAEMON_KEY'],'x'*32)
            self.assertEqual(config['GROQ_API_KEY'],'test-provider-key')

    def test_laptop_setup_keeps_output_location_and_excludes_provider_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base/'.env').write_text('ARGUS_DOCUMENTS=D:/Study\nARGUS_HUB_WS=ws://127.0.0.1:8000/ws/workstation\nARGUS_DAEMON_KEY='+'x'*32+'\nGROQ_API_KEY=test-secret\n')
            with patch.object(setup_config,'BASE',base), patch('sys.argv',['setup','--laptop']), patch('builtins.input',return_value=''), patch('getpass.getpass',return_value=''), redirect_stdout(io.StringIO()):
                setup_config.main()
            config = dotenv_values(base/'.env',interpolate=False)
            self.assertEqual(config['ARGUS_DOCUMENTS'],'D:/Study')
            self.assertEqual(config['ARGUS_DAEMON_KEY'],'x'*32)
            self.assertNotIn('GROQ_API_KEY',config)
