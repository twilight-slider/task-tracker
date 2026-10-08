$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$testRoot = Join-Path $repo ('.runtime\tests\test-persistent-worker\run-' + $PID)
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Set up root .venv with Python 3.11 before this test.' }
$worker = Join-Path $testRoot 'fake_worker.py'
@'
import json
import os
import sys
import time

VERSION = "one"
if os.path.exists(os.path.join(os.path.dirname(__file__), "do-not-read")):
    time.sleep(5)

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "die":
        os._exit(1)
    if method == "garbage":
        print("not-json", flush=True)
        continue
    if method == "sleep":
        time.sleep(2)
    if method == "unicode":
        print(json.dumps({"ok": True, "data": {"text": "Ответ 東京 مرحبا 🚀 café"}}, ensure_ascii=False), flush=True)
        continue
    print(json.dumps({"ok": True, "data": {"pid": os.getpid(), "method": method,
                                    "isolated": sys.flags.isolated, "version": VERSION}}), flush=True)
'@ | Set-Content -LiteralPath $worker -Encoding UTF8
$realWorker = Join-Path $testRoot 'folder_worker_test.py'
@'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from task_folder import FolderStore
from folder_worker import main

FolderStore._secure = lambda self, task: None  # Isolated fixture has no service ACL.
main()
'@ | Set-Content -LiteralPath $realWorker -Encoding UTF8
$trackerRoot = Join-Path $testRoot 'Tracker'
$tasksRoot = Join-Path $trackerRoot 'tasks'
$protectedRoot = Join-Path $trackerRoot '.protected'
New-Item -ItemType Directory -Path $tasksRoot, $protectedRoot -Force | Out-Null
$realConfig = Join-Path $testRoot 'worker-config.json'
@{ schemaVersion = 1; trackerRoot = $trackerRoot; tasksRoot = $tasksRoot; protectedRoot = $protectedRoot; snapshotRoots = @($repo) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $realConfig -Encoding UTF8
@{ schema_version = 1; projects = @(@{ project_key = 'TEST'; source_type = 'NO_JIRA'; next_issue_number = 1 }) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $trackerRoot 'projects.json') -Encoding UTF8
$source = Join-Path $testRoot 'PersistentWorkerTest.cs'
$exe = Join-Path $testRoot 'PersistentWorkerTest.exe'
@'
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal static class PersistentWorkerTest
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    private static string ExpectedVersion = "one";

    private static int Pid(PersistentWorker worker, string method)
    {
        var response = Json.Deserialize<Dictionary<string, object>>(worker.Call("{\"method\":\"" + method + "\",\"arguments\":{}}"));
        var data = (Dictionary<string, object>)response["data"];
        if ((string)data["method"] != method) throw new Exception("Wrong method.");
        if ((int)data["isolated"] != 1) throw new Exception("Python must run in isolated mode.");
        if ((string)data["version"] != ExpectedVersion) throw new Exception("Python code update was not loaded.");
        return (int)data["pid"];
    }

    private static void Fails(PersistentWorker worker, string method)
    {
        try { Pid(worker, method); throw new Exception("Expected failure for " + method); }
        catch (InvalidOperationException) { }
        catch (System.TimeoutException) { }
    }

    private static string Hash(string path)
    {
        using (var sha = SHA256.Create())
        using (var file = File.OpenRead(path)) return Convert.ToBase64String(sha.ComputeHash(file));
    }

    private static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static int Run(string[] args)
    {
        var protectedRoot = Path.Combine(args[2], ".protected");
        var runtimeRoot = Path.Combine(protectedRoot, "runtime");
        var binRoot = Path.Combine(protectedRoot, "bin");
        Directory.CreateDirectory(runtimeRoot);
        Directory.CreateDirectory(binRoot);
        Directory.CreateDirectory(Path.Combine(runtimeRoot, "Scripts"));
        File.WriteAllText(Path.Combine(runtimeRoot, "Scripts", "python.exe"), "test fixture");
        File.WriteAllText(Path.Combine(binRoot, "folder_worker.py"), "# test fixture");
        var configPath = Path.Combine(protectedRoot, "service.json");
        File.WriteAllText(configPath, Json.Serialize(new Dictionary<string, object> {
            { "serviceName", "test-service" }, { "serviceAccountSid", "S-1-5-18" },
            { "agentSid", "S-1-5-18" }, { "pipeName", "test-pipe" },
            { "installRoot", binRoot }, { "workerLanguage", "python" }
        }));
        var config = ServiceConfig.Load(configPath);
        if (config.PythonPath != Path.Combine(runtimeRoot, "Scripts", "python.exe") ||
            config.PythonScriptPath != Path.Combine(binRoot, "folder_worker.py"))
            throw new Exception("Python paths must derive from protected Tracker.");
        var legacyPath = Path.Combine(protectedRoot, "service-legacy.json");
        File.WriteAllText(legacyPath, Json.Serialize(new Dictionary<string, object> {
            { "serviceName", "test-service" }, { "serviceAccountSid", "S-1-5-18" },
            { "agentSid", "S-1-5-18" }, { "pipeName", "test-pipe" },
            { "installRoot", binRoot }, { "nodePath", "C:\\node.exe" }
        }));
        try { ServiceConfig.Load(legacyPath); throw new Exception("Legacy Node configuration was accepted."); }
        catch (InvalidDataException) { }
        string before = Hash(args[3]);
        using (var worker = new PersistentWorker(args[0], args[1], args[2], 500)) {
            worker.Start();
            int first = Pid(worker, "first");
            if (Pid(worker, "second") != first) throw new Exception("Worker restarted between ordinary calls.");
            Fails(worker, "die");
            int restarted = Pid(worker, "third");
            if (restarted == first) throw new Exception("Worker did not restart after crash.");
            Fails(worker, "garbage");
            Pid(worker, "fourth");
            Fails(worker, "sleep");
            Pid(worker, "fifth");
            var unicode = Json.Deserialize<Dictionary<string, object>>(worker.Call("{\"method\":\"unicode\",\"arguments\":{}}"));
            if ((string)((Dictionary<string, object>)unicode["data"])["text"] != "Ответ 東京 مرحبا 🚀 café")
                throw new Exception("Python stdout changed Unicode text.");
        }
        using (var worker = new PersistentWorker(args[0], args[4], args[5], 5000)) {
            var request = "{\"method\":\"create_local_task_folder\",\"arguments\":{\"project_key\":\"TEST\",\"title\":\"Заголовок 東京 🚀 café\",\"statement\":\"Описание مرحبا नमस्ते 中文 😀\"}}";
            var response = Json.Deserialize<Dictionary<string, object>>(worker.Call(request));
            if (!(bool)response["ok"]) throw new Exception("Unicode task creation failed: " + response["code"]);
            var data = (Dictionary<string, object>)response["data"];
            var expectedTask = "# Заголовок 東京 🚀 café" + Environment.NewLine + Environment.NewLine +
                "Описание مرحبا नमस्ते 中文 😀" + Environment.NewLine;
            if ((string)data["key"] != "TEST-1" ||
                File.ReadAllText((string)data["sourceReference"], Encoding.UTF8) != expectedTask)
                throw new Exception("Created task.md does not preserve Unicode text.");
        }
        File.WriteAllText(args[1], File.ReadAllText(args[1]).Replace("VERSION = \"one\"", "VERSION = \"two\""));
        ExpectedVersion = "two";
        File.WriteAllText(Path.Combine(args[2], "do-not-read"), "test fixture");
        using (var worker = new PersistentWorker(args[0], args[1], args[2], 500)) {
            worker.Start();
            var largeRequest = "{\"method\":\"blocked\",\"arguments\":{\"payload\":\"" +
                new string('x', 128 * 1024) + "\"}}";
            var blocked = Task.Run(() => {
                try { worker.Call(largeRequest); return "unexpected-success"; }
                catch (System.TimeoutException) { return "timeout"; }
                catch (Exception error) { return error.GetType().Name; }
            });
            if (!blocked.Wait(2000)) {
                Console.Error.WriteLine("stdin write did not time out");
                Environment.Exit(1);
            }
            if (blocked.Result != "timeout") throw new Exception("Expected stdin timeout, got " + blocked.Result);
            File.Delete(Path.Combine(args[2], "do-not-read"));
            Pid(worker, "after-blocked-write");
        }
        if (Hash(args[3]) != before) throw new Exception("Service EXE changed after Python edit.");
        Console.WriteLine("Persistent worker reuse, restart, invalid output, timeout and stable EXE passed");
        return 0;
    }
}
'@ | Set-Content -LiteralPath $source -Encoding UTF8
$csc = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /codepage:65001 /target:exe /main:PersistentWorkerTest "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs') $source
if ($LASTEXITCODE -ne 0) { throw 'Persistent worker test did not compile.' }
& $exe $python $worker $testRoot $exe $realWorker $realConfig
if ($LASTEXITCODE -ne 0) { throw 'Persistent worker test failed.' }
