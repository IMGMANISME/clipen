from pathlib import Path
import tempfile
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from clipen.providers.codex_editing import CodexEditingAgent, _PROMPT


class FakeRepository:
    pass


class CodexEditingAgentTests(unittest.TestCase):
    def test_image_bearing_success_over_two_megabytes_keeps_actual_usage(self):
        # Actual MCP runs include image events and exceed the text-only adapter
        # limit. Both terminal validation and usage recovery must use 32 MiB.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
            outcomes=[]
            finished=SimpleNamespace(provider='codex',state='succeeded',final_revision_id='editrev_'+'c'*32)
            repository=SimpleNamespace(production_session=lambda *args:None,get=lambda *args:finished,record_provider_outcome=lambda *args,**kwargs:outcomes.append(kwargs) or finished)
            agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
            def success(command,workspace,events,diagnostics,timeout,cancel):
                events.write_text(json.dumps({'type':'item.completed','image':'x'*2_100_000})+'\n'+json.dumps({'type':'turn.completed','usage':{'input_tokens':2443115,'cached_input_tokens':2342528,'output_tokens':8546}})+'\n')
                diagnostics.write_text('');return 0
            with patch.object(agent,'_run',side_effect=success):result=agent.execute('prj_'+'a'*32,'editrun_'+'b'*32)
            self.assertEqual(result.run.state,'succeeded');self.assertEqual(outcomes[0]['usage']['input_tokens'],2443115)
            self.assertNotIn('error_code',outcomes[0])

    def test_duration_budget_covers_long_form_render_and_explicit_timeout_wins(self):
        from clipen.providers.codex_editing import _PRODUCTION_PROMPT
        self.assertIn('intent.target_duration_seconds', _PRODUCTION_PROMPT)
        self.assertNotIn('Make a 30-75 second', _PRODUCTION_PROMPT)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
            finished=SimpleNamespace(provider='codex',state='succeeded',final_revision_id='editrev_'+'c'*32)
            session={'intent':{'target_duration_seconds':300}}
            repository=SimpleNamespace(production_session=lambda *args:session,get=lambda *args:finished,record_provider_outcome=lambda *args,**kwargs:finished)
            agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
            timeouts=[]
            def success(command,workspace,events,diagnostics,timeout,cancel):
                timeouts.append(timeout);events.write_text('{"type":"turn.completed"}\n');diagnostics.write_text('');return 0
            with patch.object(agent,'_run',side_effect=success):
                for target in (60,300,600,1200):
                    session['intent']['target_duration_seconds']=target
                    agent.execute('prj_'+'a'*32,'editrun_'+'b'*32)
                agent.execute('prj_'+'a'*32,'editrun_'+'b'*32,timeout_seconds=42)
            self.assertEqual(timeouts,[1200,4800,8400,14400,42])
            with self.assertRaises(ValueError):agent.execute('p','r',timeout_seconds=14401)

    def test_jsonl_usage_limit_is_saved_as_quota_failure_without_tool_replay(self):
        from clipen.providers import ProviderUsageLimitError
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
            outcomes=[]
            repository=SimpleNamespace(production_session=lambda *args:None,get=lambda *args:SimpleNamespace(provider='codex'),record_provider_outcome=lambda *args,**kwargs:outcomes.append(kwargs))
            agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
            def failed(command,workspace,events,diagnostics,timeout,cancel):
                events.write_text(json.dumps({'type':'turn.failed','error':{'message':"You've hit your usage limit."}})+'\n');diagnostics.write_text('');return 1
            with patch.object(agent,'_run',side_effect=failed) as run:
                with self.assertRaises(ProviderUsageLimitError):agent.execute('prj_'+'a'*32,'editrun_'+'b'*32)
            self.assertEqual(run.call_count,1);self.assertEqual(outcomes[0]['error_code'],'provider_usage_limit');self.assertIsNone(outcomes[0]['usage'])

    def test_unfinished_turns_say_whether_delivery_checks_held_the_draft_back(self):
        from clipen.providers.errors import ProviderDeliveryBlocked, ProviderStoppedEarly
        for blockers,error,code in ((('subtitle_source_range_unresolved',),ProviderDeliveryBlocked,'delivery_blocked'),
                                    ((),ProviderStoppedEarly,'provider_stopped_early'),(None,ProviderStoppedEarly,'provider_stopped_early')):
            with self.subTest(code=code,blockers=blockers),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
                outcomes=[]
                unfinished=SimpleNamespace(provider='codex',state='running',final_revision_id=None)
                repository=SimpleNamespace(production_session=lambda *args:None,get=lambda *args:unfinished,delivery_blockers=lambda *args:blockers,
                                           record_provider_outcome=lambda *args,**kwargs:outcomes.append(kwargs))
                agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
                def ended(command,workspace,events,diagnostics,timeout,cancel):
                    events.write_text('{"type":"turn.completed"}\n');diagnostics.write_text('');return 0
                with patch.object(agent,'_run',side_effect=ended),self.assertRaises(error) as raised:
                    agent.execute('prj_'+'a'*32,'editrun_'+'b'*32)
                self.assertEqual(outcomes[0]['error_code'],code)
                if blockers:self.assertIn('subtitle_source_range_unresolved',str(raised.exception))

    def test_the_mac_stays_awake_only_while_an_attempt_runs(self):
        import subprocess, sys
        from clipen.providers import editing_agent
        with patch.object(editing_agent.sys, 'platform', 'linux'):
            self.assertIsNone(editing_agent.keep_awake(1))
        with patch.object(editing_agent.sys, 'platform', 'darwin'), patch.object(editing_agent.os, 'access', return_value=True), \
                patch.object(editing_agent.subprocess, 'Popen') as popen:
            editing_agent.keep_awake(4321)
        self.assertEqual(popen.call_args.args[0], ['/usr/bin/caffeinate', '-i', '-s', '-w', '4321'])
        # The assertion is released when the attempt ends, whatever the outcome.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
            repository=SimpleNamespace(production_session=lambda *args:None)
            agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
            awake=SimpleNamespace(terminated=False)
            awake.terminate=lambda:setattr(awake,'terminated',True);awake.wait=lambda timeout=None:0
            with patch('clipen.providers.editing_agent.keep_awake',return_value=awake) as keep:
                code=agent._run((sys.executable,'-c','print(1)'),root,root/'events.jsonl',root/'diagnostics.log',30,None)
            self.assertEqual(code,0);keep.assert_called_once();self.assertTrue(awake.terminated)

    def test_a_sleeping_mac_is_recorded_instead_of_blamed_on_the_ai(self):
        import itertools, sys
        from clipen.providers import editing_agent
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);projects=root/'projects';projects.mkdir();attempts=root/'attempts';attempts.mkdir()
            slept=[]
            finished=SimpleNamespace(provider='codex',state='succeeded',final_revision_id='editrev_'+'c'*32)
            repository=SimpleNamespace(production_session=lambda *args:None,get=lambda *args:finished,
                                       record_provider_outcome=lambda *args,**kwargs:finished,
                                       record_machine_sleep=lambda project,run,periods:slept.extend(periods))
            agent=CodexEditingAgent(projects,attempts,repository,binary='/bin/echo',python='/usr/bin/python3',environment={'PATH':'/usr/bin','HOME':str(root)})
            # Wall-clock time jumps by two hours between two 50 ms checks: the Mac was asleep.
            clock=itertools.chain([1000.,1000.05,8200.],(8200.+n*.05 for n in itertools.count(1)))
            command=(sys.executable,'-c','import time,json;time.sleep(.4);print(json.dumps({"type":"turn.completed"}))')
            with patch.object(editing_agent,'_wall',side_effect=lambda:next(clock)),patch.object(agent,'command',return_value=command):
                agent.execute('prj_'+'a'*32,'editrun_'+'b'*32)
        self.assertEqual(slept,[(1000.05,8200.)])

    def test_prompt_gives_model_tool_choice_and_requires_observed_final_output(self):
        self.assertIn('list_editing_tools',_PROMPT)
        self.assertIn('Do not add decoration to satisfy a quota',_PROMPT)
        self.assertIn('sample_source',_PROMPT)
        self.assertIn('preview of the exact final revision',_PROMPT)
        self.assertIn('Never claim to have heard a track',_PROMPT)
        self.assertNotIn('Add one to three',_PROMPT)

    def test_command_is_ephemeral_and_registers_only_project_scoped_mcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            attempts = root / "attempts"
            workspace = attempts / "agent-test"
            projects.mkdir()
            attempts.mkdir()
            workspace.mkdir()
            agent = CodexEditingAgent(
                projects,
                attempts,
                FakeRepository(),
                binary="/bin/echo",
                python="/usr/bin/python3",
                environment={"PATH": "/usr/bin:/bin", "HOME": str(root)},
            )

            command = agent.command(
                "prj_" + "a" * 32,
                "editrun_" + "b" * 32,
                workspace,
                workspace / "final.txt",
            )
            joined = " ".join(command)

        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ask-for-approval", command)
        self.assertIn("mcp_servers.clipen.command", joined)
        self.assertIn("clipen.mcp_editing", joined)
        self.assertIn("mcp_servers.clipen.required=true", command)
        self.assertIn(
            'mcp_servers.clipen.default_tools_approval_mode="approve"',
            command,
        )
        self.assertIn("mcp_servers.clipen.enabled_tools", joined)
        self.assertIn("editrun_" + "b" * 32, joined)
        self.assertNotIn("dangerously-bypass", joined)

    def test_command_preserves_virtual_environment_python_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            attempts = root / "attempts"
            workspace = attempts / "agent-test"
            projects.mkdir()
            attempts.mkdir()
            workspace.mkdir()
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            python = venv / "bin" / "python"
            python.symlink_to("/usr/bin/python3")
            agent = CodexEditingAgent(
                projects,
                attempts,
                FakeRepository(),
                binary="/bin/echo",
                python=python,
                environment={"PATH": "/usr/bin:/bin", "HOME": str(root)},
            )

            command = agent.command(
                "prj_" + "a" * 32,
                "editrun_" + "b" * 32,
                workspace,
                workspace / "final.txt",
            )

        self.assertIn(f'mcp_servers.clipen.command="{python}"', command)
        self.assertNotIn('mcp_servers.clipen.command="/usr/bin/python3"', command)


if __name__ == "__main__":
    unittest.main()
