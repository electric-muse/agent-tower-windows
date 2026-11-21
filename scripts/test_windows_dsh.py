"""Windows dsh adapter regression without a model call or Feishu credentials."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import tower_dsh as dsh
from tower_platform import IS_WINDOWS


SID = '11111111-2222-4333-8444-555555555555'


@unittest.skipUnless(IS_WINDOWS and shutil.which('node'), 'Windows Node.js is required')
class WindowsDshTests(unittest.TestCase):
    def test_headless_stream_is_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'fake-dsh.js'
            binary.write_text(
                'let input = "";\n'
                'process.stdin.setEncoding("utf8");\n'
                'process.stdin.on("data", chunk => input += chunk);\n'
                'process.stdin.on("end", () => {\n'
                '  const flags = process.argv.slice(2).join(" ");\n'
                f'  if (!(["--profile headless --json -", '
                f'"--profile headless --json --session-id {SID} -"].includes(flags)) || '
                '!(["检查项目状态", "继续检查"].includes(input))) '
                'process.exit(7);\n'
                '  console.error("PRIVATE_DIAGNOSTIC");\n'
                '  console.log(JSON.stringify({type:"thinking", text:"PRIVATE_REASONING"}));\n'
                f'  console.log(JSON.stringify({{type:"session", sessionId:"{SID}"}}));\n'
                '  console.log(JSON.stringify({type:"final", text:"已完成"}));\n'
                '});\n', encoding='utf-8')
            for session, prompt in (('', '检查项目状态'), (SID, '继续检查')):
                output = root / ('resume.jsonl' if session else 'new.jsonl')
                with output.open('wb') as stream:
                    process = dsh.spawn(str(binary), str(root), prompt, stream,
                                        session=session, perm='safe', node_bin=shutil.which('node'))
                    self.assertEqual(process.wait(timeout=15), 0)
                content = output.read_text(encoding='utf-8')
                lines = [json.loads(line) for line in content.splitlines()]
                self.assertTrue(any(row.get('type') == 'session' and row.get('sessionId') == SID
                                    for row in lines))
                self.assertTrue(any(row.get('type') == 'final' and row.get('text') == '已完成'
                                    for row in lines))
                self.assertNotIn('PRIVATE', content)


if __name__ == '__main__':
    unittest.main()
