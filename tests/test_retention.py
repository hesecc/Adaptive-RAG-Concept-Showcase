"""The collector only operates on fresh, disposable stores in these tests."""

from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings, Principal
from adaptive_rag.domain import IngestRequest, NotFoundError
from adaptive_rag.figures import ParsedFigureRequest, figure_binding
from adaptive_rag.retention import _run_locked, UnsafeInventory, WEEK, _s3_delete
from adaptive_rag.retention_cli import directory_lock


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.app = RagApplication(Settings(data_dir=self.root))
        self.assets = self.app.assets
        self.obj = self.app.objects
        self.admin = Principal('A', 'admin', admin=True)

    def rows(self, file, query, *args):
        with closing(sqlite3.connect(self.root / file)) as con, con:
            return con.execute(query, args).fetchall()

    def collect(self, now):
        return _run_locked(self.root, {}, now)

    def doc(self, tenant, did, content, **kw):
        req = ParsedFigureRequest if 'parser_figure_keys' in kw else IngestRequest
        return self.app.ingest(req(tenant, did, 'v1', content, **kw))

    def trash_doc(self):
        row = self.app.store.trash_document(self.admin, 'doc', None)
        return datetime.fromisoformat(row['purge_after']).timestamp()

    def test_document_deadline_parsed_raw_bound_figure_traces_and_cross_catalog(self):
        fig = self.obj.put(b'bound figure')
        pdf = self.obj.put(b'%PDF-original')
        unknown = self.obj.put(b'not a parser reference')
        text = 'content ![forged](ragfig:' + unknown + ')'
        result = self.doc('A', 'doc', text, source_object_key=pdf, parser_figure_keys=(fig,))
        binding = self.obj.put(figure_binding('A', 'doc', 'v1', result.revision_id, fig))
        self.doc('B', 'doc', text)  # Parsed bytes shared across tenants.
        # A historical image revision in a THIRD tenant still owns the same figure.
        asset = dict(tenant_id='C', asset_id='x', revision_id='x1', object_key=fig, thumbnail_key=fig)
        self.rows('assets.sqlite3', 'INSERT INTO image_asset_revisions VALUES(?,?,?,?)', 'C','x','x1',json.dumps(asset))
        for tenant in ('A', 'B'):
            self.rows('knowledge.sqlite3', 'INSERT INTO traces VALUES(?,?,?)', tenant, 'trace',
                      json.dumps({'evidence':[{'document_id':'doc'}]}))
        for key, parent in [('follow-up', 'trace'), ('follow-up-2', 'follow-up')]:
            self.rows('knowledge.sqlite3', 'INSERT INTO traces VALUES(?,?,?)', 'A', key,
                      json.dumps({'history_trace_ids': [parent]}))
        deadline = self.trash_doc()
        self.assertEqual(self.collect(deadline - 0.001)['documents'], 0)
        self.assertEqual(self.collect(deadline)['documents'], 1)
        for key in (pdf, binding):
            self.assertFalse(self.obj._path(key).exists())
        for key in (fig, unknown, sha256(text.encode()).hexdigest()):
            self.assertTrue(self.obj._path(key).is_file())
        self.assertEqual(self.rows('knowledge.sqlite3','SELECT tenant_id FROM traces'), [('B',)])
        self.assertEqual(self.rows('knowledge.sqlite3','SELECT tenant_id FROM children'), [('B',)])
        self.assertEqual(self.collect(deadline), {'documents':0, 'images':0, 'unshared_objects':0})

    def test_reused_asset_incarnation_vectors_events_traces_and_live_thumbnails(self):
        db = 'assets.sqlite3'
        old = self.obj.put(b'image previous')
        thumb = self.obj.put(b'shared thumbnail')
        new = self.obj.put(b'image current')
        def body(rev, inc, obj):
            return json.dumps(dict(tenant_id='A', asset_id='x', revision_id=rev, incarnation_id=inc,
                                   object_key=obj, thumbnail_key=thumb))
        for rev, inc, obj in [('v1','first',old), ('v2','first',old), ('v3','second',new)]:
            self.rows(db, 'INSERT INTO image_asset_revisions VALUES(?,?,?,?)', 'A','x',rev,body(rev, inc, obj))
            self.rows(db, 'INSERT INTO image_events VALUES(?,?,?,?,?,?,?,?)', 'A','x',inc,rev,0,'create','owner',rev)
            self.rows('asset-vectors.sqlite3', 'INSERT INTO asset_vectors VALUES(?,?,?,?,?,?)', 'A','p',rev,'x',rev,'[1]')
            self.rows(db, 'INSERT INTO image_traces VALUES(?,?,?)', 'A',rev,json.dumps({'hits':[{'asset':{'asset_id':'x','revision_id':rev}}]}))
        self.rows(db, 'INSERT INTO image_assets VALUES(?,?,?,?,0)', 'A','x','v3',body('v3','second',new))
        self.rows(db, 'INSERT INTO image_trash VALUES(?,?,?,?,?,?,?,?)', 'A','trash','x',100,100+WEEK,'owner',0,body('v2','first',old))
        self.assertEqual(self.collect(99+WEEK)['images'], 0)
        self.assertEqual(self.collect(100+WEEK)['images'], 1)
        self.assertFalse(self.obj._path(old).exists())
        self.assertEqual(self.obj.get(thumb), b'shared thumbnail')
        self.assertEqual(self.obj.get(new), b'image current')
        for table in ('image_assets', 'image_events', 'image_asset_revisions'):
            self.assertEqual(self.rows(db, f'SELECT revision_id FROM {table}'), [('v3',)])
        self.assertEqual(self.rows(db, 'SELECT trace_id FROM image_traces'), [('v3',)])
        self.assertEqual(self.rows('asset-vectors.sqlite3','SELECT revision_id FROM asset_vectors'), [('v3',)])
        self.assertEqual(self.collect(101+WEEK)['unshared_objects'], 0)

    def test_purge_cancels_affected_candidate_but_preserves_its_other_jobs(self):
        store = self.app.store
        live = self.doc('A', 'keep', 'Live unrelated document')
        base = store.publish('A', live.job_id, None)
        remove = self.doc('A', 'doc', 'Soon to be removed')
        old = store.publish('A', remove.job_id, base.generation)
        draft = self.doc('A', 'next', 'An independent pending update')
        candidate = store.create_candidate_snapshot('A', [draft.job_id], old.generation)
        trash = store.trash_document(self.admin, 'doc', old.generation)
        self.assertEqual(self.collect(datetime.fromisoformat(trash['purge_after']).timestamp())['documents'], 1)
        self.assertEqual(dict(store.open_snapshot('A', old.generation).revisions), {'keep': live.revision_id})
        self.assertEqual(dict(store.open_snapshot('A').revisions), {'keep': live.revision_id})
        with self.assertRaises(NotFoundError):
            store.activate_candidate_snapshot('A', candidate.generation, old.generation)
        self.assertEqual(store.get_job('A', draft.job_id)['state'], 'staged')
        fresh = store.publish('A', draft.job_id, trash['generation'])
        self.assertEqual(dict(fresh.revisions), {'keep': live.revision_id, 'next': draft.revision_id})

    def test_retry_rechecks_references_after_unlink_failure(self):
        self.doc('A','doc','identical')
        deadline = self.trash_doc()
        with patch.object(Path,'unlink',side_effect=PermissionError), self.assertRaises(PermissionError):
            self.collect(deadline)
        self.doc('B','late-upload','identical')
        self.assertEqual(self.collect(deadline)['unshared_objects'], 0)
        self.assertEqual(self.obj.get(sha256(b'identical').hexdigest()), b'identical')

    def test_corrupt_retained_inventory_refuses_collection(self):
        self.doc('A','doc','destroy only with proof')
        deadline = self.trash_doc()
        self.rows('assets.sqlite3', 'INSERT INTO image_asset_revisions VALUES(?,?,?,?)', 'B','x','v','{}')
        with self.assertRaises((KeyError, UnsafeInventory)):
            self.collect(deadline)
        self.assertEqual(self.obj.get(sha256(b'destroy only with proof').hexdigest()), b'destroy only with proof')
        self.assertEqual(self.rows('knowledge.sqlite3','SELECT COUNT(*) FROM document_trash'), [(1,)])

    def test_legacy_pdf_markers_veto_but_never_authorize_blob_deletion(self):
        figure = self.obj.put(b'legacy figure')
        self.doc('A', 'doc', 'new parsed markdown', parser_figure_keys=(figure,))
        self.doc('B', 'legacy', f'![figure](ragfig:{figure})', source_object_key=self.obj.put(b'legacy pdf'))
        self.collect(self.trash_doc())
        self.assertEqual(self.obj.get(figure), b'legacy figure')

    def test_real_trashed_image_content_and_thumbnail_removed(self):
        from test_assets import Image, picture
        if Image is None:
            self.skipTest('requires Pillow')
        item = self.assets.managed_create(self.admin, asset_id='photo', content=picture(), title='unique')
        raw = self.rows('assets.sqlite3', 'SELECT body FROM image_assets')[0][0]
        keys = [json.loads(raw)[field] for field in ('object_key','thumbnail_key')]
        self.assets.trash_image(self.admin, 'photo', item['revision_id'])
        deadline = self.rows('assets.sqlite3', 'SELECT expires_at FROM image_trash')[0][0]
        self.assertEqual(self.collect(deadline)['images'], 1)
        for key in keys:
            self.assertFalse(self.obj._path(key).exists())
        for table in ('image_events','image_index_jobs','image_assets','image_asset_revisions','image_trash'):
            self.assertEqual(self.rows('assets.sqlite3', f'SELECT COUNT(*) FROM {table}'), [(0,)])

    def test_local_cloud_cache_all_namespaces_keeps_shared_keys(self):
        from adaptive_rag.cloud import CloudDenseRetriever
        from adaptive_rag.domain import ChildChunk
        self.doc('A','doc','unique')
        self.doc('A','kept','other')
        cache = 'cloud-vectors.sqlite3'
        self.rows(cache,'CREATE TABLE vectors(namespace TEXT,cache_key TEXT)')
        keys = {}
        for doc, body in self.rows('knowledge.sqlite3', 'SELECT document_id,value_json FROM children'):
            key = CloudDenseRetriever._key(ChildChunk(**json.loads(body)))
            keys[doc] = key
            for model in ('a', 'b'):
                self.rows(cache,'INSERT INTO vectors VALUES(?,?)',model,key)
        self.collect(self.trash_doc())
        self.assertEqual(self.rows(cache,'SELECT cache_key FROM vectors'), [(keys['kept'],)]*2)

    def test_mixed_deployment_and_versioned_bucket_refused(self):
        with self.assertRaises(UnsafeInventory):
            _run_locked(self.root, {'RAG_STORE_BACKEND':'postgres','RAG_POSTGRES_DSN':'not-used'}, 0)
        store = Mock()
        store.client.get_bucket_versioning.return_value = {'Status':'Suspended','ResponseMetadata':{'HTTPStatusCode':200}}
        with self.assertRaises(UnsafeInventory):
            _s3_delete(store)
        store.client.delete_object.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'requires Linux BSD flock')
    def test_open_writer_excludes_maintenance(self):
        with directory_lock(self.root, writer=True), self.assertRaises(BlockingIOError), directory_lock(self.root, writer=False):
            self.fail('maintenance lock acquired')
