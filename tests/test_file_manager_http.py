"""Tenant, ownership and trash contracts exercised via actual authenticated HTTP routes."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.config import Settings
from adaptive_rag.mineru_formats import ORIGINAL_MIME
from adaptive_rag.pdf import ParsedPdf, PdfSettings
from support import ADMIN_PASSWORD
from test_assets import picture
from test_pdf import pdf_bytes


class FileCenterHTTPTests(unittest.TestCase):
    parser_settings = None

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.api = create_api(Settings(data_dir=Path(work.name), admin_password=ADMIN_PASSWORD),
                              mineru_settings=self.parser_settings)
        self.admin = self.login('ragadmin', ADMIN_PASSWORD)
        self.alice = self.member('alice')
        self.bob = self.member('bobby')

    def login(self, user, password):
        client = TestClient(self.api)
        self.addCleanup(client.close)
        self.assertEqual(client.post('/auth/login', json={'username': user, 'password': password}).status_code, 200)
        return client

    def member(self, name):
        self.assertEqual(self.admin.post('/admin/users', json={'username': name, 'password': 'scratch-pwd'}).status_code, 201)
        return self.login(name, 'scratch-pwd')

    @staticmethod
    def body(version='v1'):
        return dict(document_id='handbook', version=version, title='Shipping guide', data_domains=['public'],
                    content='A copperlime carrier accepts signed parcels.')

    def stage(self):
        result = self.alice.post('/files/documents', json=self.body())
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def test_stale_page_cannot_stage_under_the_replacement_cookie_or_read_its_files(self):
        headers = {k: self.alice.get('/console/session').headers[k] for k in ('X-RAG-Principal', 'X-RAG-Tenant')}
        stale = self.bob.post('/files/documents', headers=headers, json=self.body())
        self.assertEqual(stale.status_code, 409)
        self.assertNotEqual(stale.headers['X-RAG-Principal'], headers['X-RAG-Principal'])
        self.assertEqual(self.bob.get('/files?view=mine').json()['files'], [])
        self.assertEqual(self.bob.post('/files/documents', json=self.body()).status_code, 201)
        self.assertEqual(self.bob.get('/files/document/handbook/content', headers=headers).status_code, 409)

    def test_upload_management_is_not_shared_query_permission(self):
        job = self.stage()
        self.assertEqual(self.admin.get('/files?view=all').json()['files'][0]['owner_name'], 'alice')
        self.assertEqual(self.alice.get('/files?view=mine').json()['files'][0]['id'], 'handbook')
        self.assertEqual(self.bob.get('/files?view=mine').json()['files'], [])
        self.assertEqual(self.bob.get('/files?view=all').status_code, 403)
        self.assertIn(self.bob.get('/files/document/handbook/content').status_code, (403, 404))
        self.assertIn(self.bob.post('/files/documents', json=self.body('v2')).status_code, (403, 404, 409))
        self.assertEqual(self.alice.post('/publish', json={'job_id': job['job_id']}).status_code, 403)
        self.assertEqual(self.bob.post('/query', json={'question': 'copperlime'}).json()['evidence'], [])
        self.assertEqual(self.alice.post('/files/documents', json=self.body() | {'data_domains':['finance']}).status_code, 403)
        self.assertEqual(self.alice.post('/files/documents', json=self.body() | {'owner_id':'somebody'}).status_code, 422)

    def test_old_links_stop_and_single_file_restore_preserves_other_publications(self):
        job = self.stage()
        gen = self.admin.post('/publish', json={'job_id': job['job_id']}).json()['generation']
        query = self.bob.post('/query', json={'question':'copperlime'}).json()
        link = f"/evidence/{gen}/{query['evidence'][0]['evidence_id']}"
        url = '/files/document/handbook'
        self.assertEqual(self.bob.get(link).status_code, 200)
        deleted = self.alice.post(url+'/trash', json={'expected_generation':gen})
        self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertEqual(self.bob.get(link).status_code,404)
        self.assertFalse(self.bob.post('/query', json={'question':'copperlime','generation':gen}).json()['evidence'])
        self.assertNotEqual(self.alice.get(url+'/content').status_code,200)
        trash = self.alice.get('/files?view=trash').json()
        tomb, = trash['files']
        self.assertTrue(tomb['can_restore'])
        self.assertEqual(tomb['deleted_by_name'], 'alice')
        extra = self.admin.post('/documents', json=self.body() | {'document_id':'other','content':'Platinum dispatch.'}).json()
        latest = self.admin.post('/publish', json={'job_id':extra['job_id'],'expected_generation':trash['generation']}).json()['generation']
        body = {'expected_generation':latest,'trash_id':tomb['trash_id']}
        self.assertEqual(self.alice.post(url+'/restore',json=body | {'expected_generation':gen}).status_code,409)
        ok = self.alice.post(url+'/restore',json=body)
        self.assertEqual(ok.status_code,200,ok.text)
        self.assertEqual({x['id'] for x in self.admin.get('/files?view=all').json()['files']},{'handbook','other'})
        self.assertEqual(self.bob.get(link).status_code,200)

    def test_admin_hold_and_account_name_reuse(self):
        self.stage()
        url='/files/document/handbook'
        resp=self.admin.post(url+'/trash',json={})
        self.assertEqual(resp.status_code,200,resp.text)
        tomb,=self.alice.get('/files?view=trash').json()['files']
        self.assertFalse(tomb['can_restore'])
        body={'trash_id':tomb['trash_id'], 'expected_generation':self.admin.get('/files').json()['generation']}
        self.assertIn(self.alice.post(url+'/restore',json=body).status_code,(403,404))
        self.assertEqual(self.admin.delete('/admin/users/alice').status_code,204)
        new_alice=self.member('alice')
        self.assertFalse(new_alice.get('/files?view=trash').json()['files'])
        self.assertEqual(self.admin.post(url+'/restore',json=body).status_code,200)
        self.assertFalse(new_alice.get('/files?view=mine').json()['files'])
        self.assertFalse(self.bob.post('/query',json={'question':'copperlime'}).json()['evidence'])

    def test_legacy_admin_actions_record_actor_and_protect_restore(self):
        v1 = self.stage()
        v2 = self.alice.post('/files/documents', json=self.body('v2') | {'content':'A revised dispatch guide.'})
        self.assertEqual(v2.status_code, 201, v2.text)
        gen = self.admin.post('/publish', json={'job_id': v1['job_id']}).json()['generation']
        self.assertEqual(self.admin.post('/documents/access', json={'document_id':'handbook','data_domains':['operations']}).status_code, 200)
        result = self.admin.post('/documents/delete', json={'document_id':'handbook','expected_generation':gen})
        self.assertEqual(result.status_code, 200, result.text)
        row = self.admin.get('/files/document/handbook').json()
        self.assertEqual(row['owner_name'], 'alice')
        self.assertTrue(row['admin_hold'])
        self.assertEqual([e['actor_name'] for e in row['events'][:3]], ['ragadmin'] * 3)
        self.assertEqual([e['action'] for e in row['events'][:3]], ['trash','set_access','publish'])

    def test_image_owner_exclusion_and_admin_hold_across_old_and_new_routes(self):
        image = picture()
        body = {'title':'Return desk', 'asset_id':'desk', 'content_base64':base64.b64encode(image).decode()}
        created = self.alice.post('/files/images', json=body)
        self.assertEqual(created.status_code, 201, created.text)
        url = '/files/image/desk'
        self.assertEqual(self.admin.get(url+'/content').content, image)
        self.assertEqual(self.bob.get(url).status_code, 404)
        self.assertEqual(self.bob.get('/assets/desk/content').status_code, 404)
        expected = {'expected_revision':created.json()['revision_id']}
        self.assertEqual(self.bob.post(url+'/trash',json=expected).status_code, 404)
        self.assertEqual(self.admin.delete('/assets/desk',params=expected).status_code, 204)
        row, = self.alice.get('/files?view=trash').json()['files']
        self.assertFalse(row['can_restore'])
        self.assertEqual(self.alice.get(url+'/content').status_code, 404)
        self.assertEqual(self.alice.post(url+'/restore',json={'trash_id':row['trash_id']}).status_code, 403)
        self.assertEqual(self.admin.post(url+'/restore',json={'trash_id':row['trash_id']}).status_code, 200)
        self.assertEqual(self.alice.get(url+'/content').content, image)

    def test_pdf_keeps_original_and_exposes_only_plain_text_preview(self):
        pdf = pdf_bytes(('The return office is located in room nine.',))
        resp=self.alice.post('/files/pdf', json={'document_id':'manual','version':'v1','title':'Manual',
            'content_base64':base64.b64encode(pdf).decode()})
        self.assertEqual(resp.status_code,201,resp.text)
        url='/files/document/manual/content'
        original=self.alice.get(url+'?original=true')
        self.assertEqual(original.content,pdf)
        self.assertEqual(original.headers['content-disposition'],'attachment')
        self.assertIn('return office',self.alice.get(url).text)
        self.assertIn(self.bob.get(url+'?original=true').status_code,(403,404))
        body=self.body() | {'media_type':'text/html','content':'<p>new branch</p>'}
        self.assertEqual(self.alice.post('/files/documents',json=body).status_code,201)
        self.assertTrue(self.alice.get('/files/document/handbook/content').headers['content-type'].startswith('text/plain'))

    def test_cloud_parse_without_credentials_never_sends(self):
        with patch('adaptive_rag.pdf.MinerUPdfParser.parse_original') as call:
            result = self.alice.post('/files/parse', json=cloud_body())
            self.assertEqual(result.status_code, 503, result.text)
            call.assert_not_called()


def cloud_body():
    return dict(document_id='prices', title='Service prices', version='v1', extension='xlsx',
                data_domains=['public'], content_base64=base64.b64encode(b'test workbook').decode(),
                cloud_consent=True)


class CloudFileHTTPTests(unittest.TestCase):
    setUp = FileCenterHTTPTests.setUp
    login = FileCenterHTTPTests.login
    member = FileCenterHTTPTests.member
    parser_settings = PdfSettings(api_key='unit-test-only')
    parsed = ParsedPdf('| service | fee |\n|---|---|\n| copperlime | 728 |', None, 'mineru:xlsx', 0)

    def test_denied_uploads_do_not_call_parser(self):
        body = cloud_body()
        with patch('adaptive_rag.pdf.MinerUPdfParser.parse_original') as call, TestClient(self.api) as anon:
            for client, changes, expected in ((anon, {}, 401), (self.alice, {'cloud_consent':False}, 400),
                    (self.alice, {'extension':'csv'}, 422), (self.alice, {'data_domains':['finance']}, 403)):
                with self.subTest(changes=changes):
                    result = client.post('/files/parse', json=body | changes)
                    self.assertEqual(result.status_code, expected, result.text)
            self.assertEqual(self.bob.post('/files/documents', json=FileCenterHTTPTests.body() | {'document_id':'prices'}).status_code, 201)
            self.assertEqual(self.alice.post('/files/parse', json=body).status_code, 409)
            self.assertEqual(self.bob.post('/files/document/prices/trash', json={}).status_code, 200)
            self.assertEqual(self.bob.post('/files/parse', json=body).status_code, 409)
            call.assert_not_called()

    def test_permissions_change_during_cloud_parse(self):
        def change_permissions(path):
            changed = self.admin.patch('/admin/users/alice/domains', json={'data_domains':['operations']})
            self.assertEqual(changed.status_code, 200, changed.text)
            return self.parsed, path.read_bytes()
        with patch('adaptive_rag.pdf.MinerUPdfParser.parse_original', side_effect=change_permissions), \
                patch.object(self.api.state.application.objects, 'put') as persist:
            result = self.alice.post('/files/parse', json=cloud_body())
            self.assertIn(result.status_code, (401, 409), result.text)
            persist.assert_not_called()
        self.assertFalse(self.admin.get('/files?view=all').json()['files'])

    def test_parsed_table_stage_publish_download_trash_restore(self):
        body = cloud_body()
        raw = base64.b64decode(body['content_base64'])
        with patch('adaptive_rag.pdf.MinerUPdfParser.parse_original', return_value=(self.parsed, raw)):
            job = self.alice.post('/files/parse', json=body)
        self.assertEqual(job.status_code, 201, job.text)
        path = '/files/document/prices'
        detail = self.alice.get(path).json()
        self.assertEqual(detail['source_extension'], 'xlsx')
        self.assertEqual(detail['versions'][0]['source_extension'], 'xlsx')
        original = self.alice.get(path + '/content?original=true')
        self.assertEqual(original.content, raw)
        self.assertEqual(original.headers['content-type'], ORIGINAL_MIME['xlsx'])
        self.assertEqual(original.headers['content-disposition'], 'attachment')
        self.assertIn(self.bob.get(path + '/content?original=true').status_code, (403, 404))
        preview = self.alice.get(path + '/content')
        self.assertTrue(preview.headers['content-type'].startswith('text/plain'))
        self.assertIn('728', preview.text)
        def evidence():
            return self.bob.post('/query', json={'question':'copperlime'}).json()['evidence']
        self.assertFalse(evidence())
        gen = self.admin.post('/publish', json={'job_id':job.json()['job_id']}).json()['generation']
        self.assertIn('728', evidence()[0]['text'])
        self.assertEqual(self.alice.post(path + '/trash', json={'expected_generation':gen}).status_code, 200)
        self.assertFalse(evidence())
        trash = self.alice.get('/files?view=trash').json()
        restored = self.alice.post(path + '/restore', json={'expected_generation':trash['generation'],
                                   'trash_id':trash['files'][0]['trash_id']})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIn('728', evidence()[0]['text'])
