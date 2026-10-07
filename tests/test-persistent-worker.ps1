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
    print(json.dumps({"ok": True, "data": {"pid": os.getpid(), "method": method,
                                    "isolated": sys.flags.isolated, "version": VERSION}}), flush=True)
'@ | Set-Content -LiteralPath $worker -Encoding UTF8
$source = Join-Path $testRoot 'PersistentWorkerTest.cs'
$exe = Join-Path $testRoot 'PersistentWorkerTest.exe'
@'
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
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
& $csc /nologo /target:exe /main:PersistentWorkerTest "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs') $source
if ($LASTEXITCODE -ne 0) { throw 'Persistent worker test did not compile.' }
& $exe $python $worker $testRoot $exe
if ($LASTEXITCODE -ne 0) { throw 'Persistent worker test failed.' }
