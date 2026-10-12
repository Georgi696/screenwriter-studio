"""Approval and paid-request recovery regressions. All provider calls are mocked."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from screenwriter_studio.images.generator import run_batch
from screenwriter_studio.images.inspection import Inspection, inspect_image, response_text

REPAIR = dict(verdict="repair", summary="Wrong wardrobe", findings=["The coat is blue instead of red."],
              changes=["Make the coat red."], preserve=["Keep the face and framing."], uncertainty="")
ACCEPT = dict(verdict="accept", summary="Accurate enough", findings=[], changes=[], preserve=[], uncertainty="")
PNG = b'\x89PNG\r\n\x1a\n' + b'image bytes'


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.spec = {"inspection": {"mode": "manual"}, "aspect_ratio": "9:16", "resolution": "1K",
                     "inspection_context": {"brief": "Red coat", "shots": [{"number": 1}]},
                     "jobs": [{"id": "actor", "kind": "character", "prompt": "Actor wearing red coat"},
                              {"id": "s01", "kind": "keyframe", "prompt": "Actor at doorway", "references": ["actor"]}]}
        self.count = 0
        def generate(key, payload, *, task_id=None, on_submitted=None, cancel=None):
            self.count += 1
            task = task_id or f'task-{self.count}'
            on_submitted(task)
            return {"task_id": task, "url": f"https://example.test/{task}.png"}
        self.generate = self.enterContext(patch('screenwriter_studio.images.generator.generate_one', side_effect=generate))
        self.enterContext(patch('screenwriter_studio.images.generator.load_api_key', return_value='fake-key'))
        self.enterContext(patch('screenwriter_studio.images.generator.download', side_effect=lambda url, dest: dest.write_bytes(PNG)))
        self.inspect = self.enterContext(patch('screenwriter_studio.images.review.inspect_image', return_value=REPAIR))

    def run_batch(self, **kwargs):
        return run_batch(self.spec, self.out, **kwargs)

    def decision(self, record, action):
        return {"id": record['id'], "review_id": record['review_id'], "action": action}

    def test_manual_holds_repairs_and_dependent_shots_across_resume(self):
        first = self.run_batch()
        self.assertEqual(self.generate.call_count, 1)
        actor, shot = first['images']
        self.assertEqual(actor['state'], 'review')
        self.assertEqual(shot['state'], 'blocked')
        self.assertEqual(first['status'], 'paused')
        self.assertIn('Make the coat red.', actor['repair_job']['prompt'])
        second = self.run_batch(resume=True)
        self.assertEqual(second['images'][0]['review_id'], actor['review_id'])
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(self.inspect.call_count, 1)

    def test_approved_repair_preserves_original_and_uses_exact_proposal(self):
        actor = self.run_batch()['images'][0]
        original = Path(actor['path']).read_bytes()
        self.inspect.return_value = ACCEPT
        result = self.run_batch(resume=True, decision=self.decision(actor, 'repair'))
        fixed = result['images'][0]
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(Path(actor['path']).read_bytes(), original)
        self.assertNotEqual(fixed['path'], actor['path'])
        self.assertEqual(fixed['history'][0]['review_id'], actor['review_id'])
        self.assertEqual(fixed['repairs'], 1)
        self.assertEqual(self.generate.call_args_list[1].args[1]['input']['prompt'], actor['repair_job']['prompt'])
        self.assertEqual(self.inspect.call_args_list[-1].args[3], [('actor', Path(fixed['path']))])
        before = self.generate.call_count
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_batch(resume=True, decision=self.decision(actor, 'repair'))
        self.assertEqual(self.generate.call_count, before)

    def test_resume_of_broken_last_image_returns_and_keep_needs_no_provider(self):
        self.spec["jobs"] = [self.spec["jobs"][0]]
        self.inspect.return_value = {**ACCEPT, "uncertainty": "visible through the smoke."}
        first = self.run_batch()
        image = first["images"][0]
        self.assertEqual(image["state"], "review")
        self.assertEqual(image["inspection"]["summary"], "Inspection unavailable. Human review required.")
        self.assertIn("require review", image["inspection"]["uncertainty"])
        self.assertTrue(Path(image["path"]).is_file())
        original = Path(image["path"]).read_bytes()
        gate = threading.Event()

        def block(*args, **kwargs):
            gate.wait(5)
            raise AssertionError("resume waited on a provider while review was already saved")

        self.inspect.side_effect = block
        self.generate.side_effect = block
        started = time.monotonic()
        paused = self.run_batch(resume=True)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["images"][0]["state"], "review")
        self.assertEqual(Path(image["path"]).read_bytes(), original)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        manifest["images"][0]["review_id"] = ""
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.generate.side_effect = None
        self.inspect.side_effect = None
        self.inspect.return_value = ACCEPT
        before = self.generate.call_count
        kept = self.run_batch(resume=True, decision={"id": image["id"], "review_id": "", "action": "keep"})
        self.assertEqual(self.generate.call_count, before)
        self.assertEqual(kept["status"], "complete")
        self.assertEqual(kept["images"][0]["state"], "done")
        self.assertEqual(kept["images"][0]["decision"], "human accepted")
        self.assertEqual(Path(image["path"]).read_bytes(), original)

    def test_keep_unblocks_without_repair(self):
        actor = self.run_batch()['images'][0]
        original = Path(actor['path']).read_bytes()
        repair_prompt = actor['repair_job']['prompt']
        self.inspect.return_value = ACCEPT
        result = self.run_batch(resume=True, decision=self.decision(actor, 'keep'))
        kept = result['images'][0]
        self.assertEqual(self.generate.call_count, 2)
        self.assertEqual(kept['decision'], 'human accepted')
        self.assertEqual(kept['state'], 'done')
        self.assertEqual(kept.get('repairs', 0), 0)
        self.assertEqual(Path(actor['path']).read_bytes(), original)
        prompts = [call.args[1]['input']['prompt'] for call in self.generate.call_args_list]
        self.assertNotIn(repair_prompt, prompts)
        self.assertEqual(result['images'][1]['references'], [actor['url']])

    def test_auto_repairs_stop_at_two_and_do_not_unblock_references(self):
        self.spec['inspection']['mode'] = 'auto'
        result = self.run_batch()
        actor, shot = result['images']
        self.assertEqual(self.generate.call_count, 3)
        self.assertEqual(self.inspect.call_count, 3)
        self.assertEqual(actor['repairs'], 2)
        self.assertEqual(len(actor['history']), 2)
        self.assertNotIn('repair_job', actor)
        self.assertEqual(shot['state'], 'blocked')
        self.run_batch(resume=True)
        self.assertEqual(self.generate.call_count, 3)

    def test_auto_uncertainty_and_inspection_failure_need_human(self):
        self.spec['inspection']['mode'] = 'auto'
        self.inspect.side_effect = RuntimeError('fake-key unsupported vision input')
        result = self.run_batch()
        actor = result['images'][0]
        self.assertEqual(actor['state'], 'review')
        self.assertNotIn('fake-key', json.dumps(actor))
        self.assertNotIn('repair_job', actor)
        self.inspect.side_effect = None
        self.inspect.return_value = {**REPAIR, 'uncertainty': 'The hand is partially hidden.'}
        result = self.run_batch(resume=True, decision=self.decision(actor, 'inspect'))
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(result['images'][0]['state'], 'review')

    def test_repair_timeout_resumes_same_provider_task(self):
        actor = self.run_batch()['images'][0]
        def timeout(key, payload, *, on_submitted, **kwargs):
            on_submitted('repair-task')
            raise RuntimeError('poll timeout')
        self.generate.side_effect = timeout
        self.run_batch(resume=True, decision=self.decision(actor, 'repair'))
        saved = json.loads((self.out/'manifest.json').read_text())['images'][0]
        self.assertEqual(saved['task_id'], 'repair-task')
        self.assertEqual(saved['repairs'], 1)
        def complete(key, payload, *, task_id=None, **kwargs):
            self.assertEqual(task_id, 'repair-task')
            return {'task_id': task_id, 'url': 'https://example.test/repaired.png'}
        self.generate.side_effect = complete
        # Keep the repaired version pending so no downstream generation enters this mock.
        resumed = self.run_batch(resume=True)['images'][0]
        self.assertEqual(resumed['state'], 'review')
        self.assertEqual(resumed['repairs'], 1)

    def test_oversized_reference_set_rejected_before_spend(self):
        self.spec['jobs'][0]['references'] = [f'https://example.test/{n}.png' for n in range(11)]
        with self.assertRaisesRegex(ValueError, 'reference count'):
            self.run_batch()
        self.generate.assert_not_called()

    def test_cancel_during_inspection_saves_review_without_repair(self):
        self.spec['inspection']['mode'] = 'auto'
        cancel = threading.Event()
        def inspect(*args, **kwargs):
            cancel.set()
            return REPAIR
        self.inspect.side_effect = inspect
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_batch(cancel=cancel)
        saved = json.loads((self.out/'manifest.json').read_text())['images'][0]
        self.assertEqual(saved['state'], 'review')
        self.assertEqual(self.generate.call_count, 1)

    def test_dry_run_never_inspects_or_generates(self):
        self.run_batch(dry_run=True)
        self.inspect.assert_not_called()
        self.generate.assert_not_called()

    def test_repair_filename_collision_rejected_before_spend(self):
        self.spec['jobs'][1]['filename'] = 'actor.repair-1.png'
        with self.assertRaisesRegex(ValueError, 'collide'):
            self.run_batch()
        self.generate.assert_not_called()

    def test_inspector_receives_actual_pixels_and_context(self):
        self.out.joinpath('candidate.png').write_bytes(PNG)
        self.out.joinpath('ref.png').write_bytes(PNG)
        client = Mock()
        client.responses.create.return_value = {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(ACCEPT)}]}]}
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)
        with patch('screenwriter_studio.images.inspection.kie_responses_client', return_value=manager):
            result = inspect_image({'path': str(self.out/'candidate.png')}, self.spec['jobs'][1],
                                   self.spec['inspection_context'], [('actor', self.out/'ref.png')], api_key='fake')
        self.assertEqual(result, ACCEPT)
        content = client.responses.create.call_args.kwargs['input'][0]['content']
        images = [c for c in content if c['type'] == 'input_image']
        self.assertEqual(len(images), 2)
        self.assertTrue(all(c['image_url'].startswith('data:image/png;base64,') for c in images))
        self.assertIn('Red coat', content[0]['text'])


class InspectionResponseTest(unittest.TestCase):
    def test_incomplete_stream_never_accepted_even_with_valid_json(self):
        partial = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': json.dumps(ACCEPT)})
        with self.assertRaisesRegex(ValueError, 'complete'):
            response_text(partial)

    def test_completed_sse_and_failed_status(self):
        result = {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(ACCEPT)}]}]}
        self.assertEqual(json.loads(response_text('data: ' + json.dumps({'type': 'response.completed', 'response': result}))), ACCEPT)
        result['status'] = 'incomplete'
        with self.assertRaises(ValueError):
            response_text(result)

    def test_contradictory_verdict_is_invalid(self):
        with self.assertRaises(ValueError):
            Inspection.model_validate({**REPAIR, 'verdict': 'accept'})
