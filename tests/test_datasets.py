"""Authenticated bulk registration, restart consistency, and legacy/search contract tests."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from app.config import Settings
from app import datasets
from app.main import create_app
from app.store import StoreError


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root / 'legacy', dataset_dir=self.root / 'enrolled', api_key='search-token', enrollment_api_key='enroll-token')
        self.client = TestClient(create_app(self.settings), headers={'X-API-Key': 'search-token'})
        self.client.__enter__()
        self.body = {
            'dataset_id': str(uuid.uuid4()), 'name': 'Synthetic fixture', 'scheme': 'plaintext',
            'transformer_hash': 'a' * 64, 'dimension': 2, 'records': 3,
            'ids': ['R1', 'R2', 'R3'], 'vectors': [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]],
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def register(self, body=None):
        response = self.client.post('/v1/datasets', json=body or self.body, headers={'X-API-Key': 'enroll-token'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def query(self, **updates):
        body = {key: self.body[key] for key in ('dataset_id', 'transformer_hash', 'scheme')}
        body.update(vector=[1.0, 0.0], top_k=3)
        body.update(updates)
        return self.client.post('/v1/search', json=body)

    def test_register_cosine_and_restore(self):
        response = self.register()
        self.assertEqual(response['active']['records'], 3)
        self.assertFalse(response['encrypted'])
        result = self.query().json()
        self.assertEqual(result['dataset_id'], self.body['dataset_id'])
        self.assertEqual([row['record_id'] for row in result['results']], ['R1', 'R3', 'R2'])
        self.assertEqual([row['score'] for row in result['results']], [1.0, 0.6, 0.0])
        with TestClient(create_app(self.settings), headers={'X-API-Key': 'search-token'}) as client:
            self.assertEqual(client.get('/v1/meta').json()['active'], response['active'])
        persisted = json.loads((self.settings.dataset_dir / 'snapshots' / f"{self.body['dataset_id']}.json").read_text())
        self.assertEqual(set(persisted), {'manifest', 'ids', 'vectors'})
        meta = self.client.get('/v1/meta').json()
        self.assertEqual(meta['dataset'], {'name': 'Synthetic fixture', 'records': 3, 'dimension': 2})
        self.assertFalse(meta['schemes'][1]['connected'])

    def test_missing_stale_and_incomplete_snapshot_references(self):
        self.register()
        self.assertEqual(self.query(transformer_hash='b' * 64).status_code, 409)
        self.assertEqual(self.query(dataset_id=str(uuid.uuid4())).status_code, 409)
        missing = self.query(dataset_id=None, transformer_hash=None)
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()['error']['code'], 'dataset_changed')
        self.assertEqual(self.query(dataset_id=None).status_code, 422)
        replacement = {**self.body, 'dataset_id': str(uuid.uuid4())}
        self.register(replacement)
        self.assertEqual(self.query().status_code, 409)
        self.assertEqual(self.query(dataset_id=replacement['dataset_id']).status_code, 200)

    def test_invalid_enrollment_preserves_active(self):
        self.register()
        variants = [
            {'ids': ['R1', 'R1', 'R3']}, {'records': 2}, {'dimension': 3},
            {'vectors': [[2.0, 0.0]] * 3}, {'vectors': [[1.0, 0.0], [1.0], [0.0, 1.0]]},
            {'raw_patient_name': 'PRIVATE_TEST_VALUE'}, {'ids': ['../file', 'R2', 'R3']},
            {'vectors': [[True, False]] * 3}, {'name': ' '}, {'name': 'invalid\x00name'},
        ]
        for updates in variants:
            with self.subTest(updates=updates):
                body = {**copy.deepcopy(self.body), 'dataset_id': str(uuid.uuid4()), **updates}
                response = self.client.post('/v1/datasets', json=body, headers={'X-API-Key': 'enroll-token'})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn('PRIVATE_TEST_VALUE', response.text)
                self.assertEqual(self.query().status_code, 200)

    def test_atomic_activation_failure_preserves_old_snapshot(self):
        self.register()
        original = datasets.atomic_json
        def fail_pointer(path, value):
            if path.name == 'active.json':
                raise OSError('disk failure')
            return original(path, value)
        with patch.object(datasets, 'atomic_json', side_effect=fail_pointer):
            response = self.client.post('/v1/datasets', json={**self.body, 'dataset_id': str(uuid.uuid4())}, headers={'X-API-Key': 'enroll-token'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.query().status_code, 200)
        with TestClient(create_app(self.settings), headers={'X-API-Key': 'search-token'}) as client:
            self.assertEqual(client.get('/v1/meta').json()['active']['dataset_id'], self.body['dataset_id'])

    def test_immutable_and_encryption_explicit(self):
        self.register()
        self.assertEqual(self.client.post('/v1/datasets', json=self.body, headers={'X-API-Key': 'enroll-token'}).status_code, 409)
        encrypted = {**self.body, 'dataset_id': str(uuid.uuid4()), 'scheme': 'ckks-hec3'}
        self.assertEqual(self.client.post('/v1/datasets', json=encrypted, headers={'X-API-Key': 'enroll-token'}).status_code, 501)
        self.assertEqual(self.client.post('/v1/search', json={'scheme':'ckks-hec3', 'payload':'Y2lwaGVydGV4dA=='}).status_code, 501)
        self.assertEqual(self.query().status_code, 200)

    def test_auth_origin_and_empty_database(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/v1/meta', headers={'X-API-Key': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/v1/datasets', json=self.body).status_code, 401)
        for browser_headers in ({'Origin':'http://evil.example'}, {'Sec-Fetch-Site':'same-origin'}):
            headers = {'X-API-Key': 'enroll-token', **browser_headers}
            self.assertEqual(self.client.post('/v1/datasets', json=self.body, headers=headers).status_code, 403)
        meta = self.client.get('/v1/meta').json()
        self.assertIsNone(meta['active'])
        self.assertIsNone(meta['dataset'])
        self.assertFalse(meta['schemes'][0]['connected'])
        self.assertEqual(self.query().status_code, 409)

    def test_api_key_can_enable_enrollment_without_separate_key(self):
        settings = Settings(data_dir=self.root/'empty', dataset_dir=self.root/'fallback', api_key='one-token')
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.post('/v1/datasets', json=self.body).status_code, 401)
            self.assertEqual(client.post('/v1/datasets', json=self.body, headers={'X-API-Key':'one-token'}).status_code, 201)

    def test_no_enrollment_key_never_allows_public_writes(self):
        self.register()
        settings = Settings(data_dir=self.root/'empty', dataset_dir=self.settings.dataset_dir)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.get('/v1/meta').status_code, 200)
            response = client.post('/v1/datasets', json={**self.body, 'dataset_id':str(uuid.uuid4())})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['error']['code'], 'token_not_configured')

    def test_actual_chunked_body_limit_and_private_errors(self):
        settings = Settings(data_dir=self.root/'empty', dataset_dir=self.root/'limited', api_key='token', max_body_bytes=256)
        with TestClient(create_app(settings)) as client:
            response = client.post('/v1/datasets', content=iter([b'x'*200, b'x'*200]), headers={'X-API-Key':'token'})
            self.assertEqual(response.status_code, 413)
        self.register()
        for vector in ([True, False], ['PRIVATE_TEST_VALUE', 0], [0.0, 0.0], [1.0]):
            response = self.query(vector=vector)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn('PRIVATE_TEST_VALUE', response.text)
        self.assertEqual(self.query().headers['cache-control'], 'no-store')

    def test_partial_or_malformed_legacy_database_is_not_empty(self):
        self.settings.data_dir.mkdir()
        np.save(self.settings.data_dir/'database.npy', np.array([[1.0,0.0]]))
        with self.assertRaises(StoreError):
            with TestClient(create_app(self.settings)):
                pass
        (self.settings.data_dir/'database_ids.csv').write_text('record_id\nR1\n')
        np.save(self.settings.data_dir/'database.npy', np.array([[2.0,0.0]]))
        with self.assertRaises(StoreError):
            with TestClient(create_app(self.settings)):
                pass

    def test_legacy_search_then_enrollment_switch(self):
        self.settings.data_dir.mkdir()
        np.save(self.settings.data_dir/'database.npy', np.array([[1.0,0.0], [0.0,1.0]]))
        (self.settings.data_dir/'database_ids.csv').write_text('record_id\nOLD1\nOLD2\n')
        with TestClient(create_app(self.settings), headers={'X-API-Key':'search-token'}) as client:
            query = {'scheme':'plaintext','vector':[1.0,0.0],'top_k':3}
            old = client.post('/v1/search', json=query)
            self.assertEqual(old.status_code, 200)
            self.assertNotIn('dataset_id', old.json())
            self.assertEqual(old.json()['results'][0]['record_id'], 'OLD1')
            self.assertIsNone(client.get('/v1/meta').json()['active'])
            enrolled = client.post('/v1/datasets', json=self.body, headers={'X-API-Key':'enroll-token'})
            self.assertEqual(enrolled.status_code, 201)
            self.assertEqual(client.post('/v1/search', json=query).status_code, 409)
            query.update(dataset_id=self.body['dataset_id'],transformer_hash=self.body['transformer_hash'])
            self.assertEqual(client.post('/v1/search',json=query).json()['results'][0]['record_id'],'R1')

    def test_corrupt_active_snapshot_fails_startup(self):
        self.register()
        (self.settings.dataset_dir/'active.json').write_text('{"dataset_id":"../bad"}')
        with self.assertRaises(RuntimeError):
            with TestClient(create_app(self.settings)):
                pass

    def test_restore_rejects_extra_duplicate_id(self):
        self.register()
        path = self.settings.dataset_dir/'snapshots'/f"{self.body['dataset_id']}.json"
        content = json.loads(path.read_text())
        content['ids'].append(content['ids'][0])
        path.write_text(json.dumps(content))
        with self.assertRaises(RuntimeError):
            with TestClient(create_app(self.settings)):
                pass


if __name__ == '__main__':
    unittest.main()
