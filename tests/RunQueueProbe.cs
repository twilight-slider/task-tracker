using System;
using System.IO;
using System.IO.Pipes;
using System.Diagnostics;
using System.Reflection;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

internal static class RunQueueProbe
{
    private static readonly string SameHash = new string('a', 64);
    private static readonly string DifferentHash = new string('b', 64);
    private static void Check(bool value, string message) { if (!value) throw new Exception(message); }

    private static int Main(string[] args)
    {
        try {
            var path = args[0];
            var release = new ManualResetEventSlim(false);
            var started = new CountdownEvent(2);
            var queue = new CodexRunQueue(path, 2, 3);
            Func<string, Task> work = id => Task.Run(() => { started.Signal(); release.Wait(); });
            var first = queue.Start("first", SameHash, work);
            var second = queue.Start("second", SameHash, work);
            Check(started.Wait(5000), "Two independent runs did not start in parallel.");
            Check(queue.Get(first).Status == "running" && queue.Get(second).Status == "running",
                "Active runs were not visible.");
            Check(queue.Start("first", SameHash, id => { throw new Exception("Duplicate executed."); }) == first,
                "Retried request created a second run.");
            try { queue.Start("first", DifferentHash, work); throw new Exception("Conflicting retry was accepted."); }
            catch (InvalidOperationException) { }
            try { queue.Start("invalid", "raw prompt", work); throw new Exception("Raw request data was accepted as a digest."); }
            catch (ArgumentException) { }
            var third = queue.Start("third", SameHash, id => Task.FromResult(0));
            Check(queue.Get(third).Status == "queued", "Parallelism limit was not enforced.");
            try { queue.Start("fourth", SameHash, id => Task.FromResult(0)); throw new Exception("Queue accepted a fourth pending run."); }
            catch (InvalidOperationException) { }
            Check(queue.List().Length == 3 && queue.Get("missing") == null, "Inventory failed.");
            release.Set();
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (DateTime.UtcNow < deadline &&
                (queue.Get(first).Status != "completed" || queue.Get(second).Status != "completed" ||
                 queue.Get(third).Status != "completed")) Thread.Sleep(20);
            Check(queue.Get(first).Status == "completed" && queue.Get(second).Status == "completed" &&
                queue.Get(third).Status == "completed", "Runs did not finish.");
            var failed = queue.Start("failed", SameHash, id => { throw new InvalidOperationException("Expected test failure."); });
            deadline = DateTime.UtcNow.AddSeconds(5);
            while (DateTime.UtcNow < deadline && queue.Get(failed).Status != "failed") Thread.Sleep(20);
            Check(queue.Get(failed).Status == "failed", "Known executor failure was marked uncertain.");
            var reopened = new CodexRunQueue(path, 2, 3);
            Check(reopened.List().Length == 4 && reopened.Get(first).Status == "completed" &&
                reopened.Get(failed).Status == "failed",
                "Persisted run IDs were lost.");
            Check(reopened.Start("first", SameHash, id => { throw new Exception("Recovered retry executed."); }) == first,
                "Request identity was lost after restart.");
            var saved = File.ReadAllText(path).Replace("\"completed\"", "\"running\"");
            File.WriteAllText(path, saved);
            var recovered = new CodexRunQueue(path, 2, 3);
            Check(recovered.Get(first).Status == "needs_attention" &&
                recovered.Get(second).Status == "needs_attention" &&
                recovered.Get(third).Status == "needs_attention", "Interrupted runs were replayed or hidden.");
            ServiceProbe(args[1], args[2], args[3]);
            Console.WriteLine("Parallel queue, capacity, durable IDs, and interrupted-run recovery passed.");
            return 0;
        } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static void ServiceProbe(string python, string worker, string configPath)
    {
        File.WriteAllText(configPath, "{}");
        var sid = WindowsIdentity.GetCurrent().User.Value;
        var config = new ServiceConfig {
            ServiceName = "AIDEV57Test", ServiceSid = sid, AgentSid = sid,
            PipeName = "aidev57-test-" + Guid.NewGuid().ToString("N"),
            ConfigPath = configPath, PythonPath = python, PythonScriptPath = worker,
            LogPath = Path.Combine(Path.GetDirectoryName(configPath), "service.log")
        };
        using (var service = new TaskFolderService(config)) {
            var start = typeof(TaskFolderService).GetMethod("OnStart", BindingFlags.NonPublic | BindingFlags.Instance);
            var stop = typeof(TaskFolderService).GetMethod("OnStop", BindingFlags.NonPublic | BindingFlags.Instance);
            start.Invoke(service, new object[] { new string[0] });
            try {
                var began = new CountdownEvent(2);
                var release = new ManualResetEventSlim(false);
                Func<string, Task> longRun = id => Task.Run(() => { began.Signal(); release.Wait(); });
                var clock = Stopwatch.StartNew();
                var first = service.StartCodexRun("service-first", SameHash, longRun);
                var second = service.StartCodexRun("service-second", SameHash, longRun);
                Check(clock.ElapsedMilliseconds < 1000, "Service did not return IDs promptly.");
                Check(began.Wait(5000), "Service did not start two runs concurrently.");
                Check(service.ListCodexRuns().Length == 2, "Service run inventory failed.");
                using (var pipe = new NamedPipeClientStream(".", config.PipeName, PipeDirection.InOut)) {
                    pipe.Connect(5000);
                    using (var writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, true) { AutoFlush = true })
                    using (var reader = new StreamReader(pipe, Encoding.UTF8, false, 4096, true)) {
                        var folderClock = Stopwatch.StartNew();
                        writer.WriteLine("{\"method\":\"folder_probe\",\"arguments\":{}}");
                        var response = reader.ReadLine();
                        Check(response != null && response.Contains("folder_probe"), "Folder worker did not respond: " + response);
                        Check(folderClock.ElapsedMilliseconds < 2000, "Long runs blocked the folder worker.");
                    }
                }
                var remaining = 31000 - (int)clock.ElapsedMilliseconds;
                if (remaining > 0) Thread.Sleep(remaining);
                release.Set();
                var deadline = DateTime.UtcNow.AddSeconds(5);
                while (DateTime.UtcNow < deadline &&
                    (service.GetCodexRun(first).Status != "completed" || service.GetCodexRun(second).Status != "completed"))
                    Thread.Sleep(20);
                Check(service.GetCodexRun(first).Status == "completed" &&
                    service.GetCodexRun(second).Status == "completed", "Service runs did not complete.");
                Console.WriteLine("Two service runs exceeded 30 seconds while the folder worker responded.");
            } finally { stop.Invoke(service, null); }
        }
    }
}
