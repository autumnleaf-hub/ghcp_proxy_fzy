using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace BpsManager
{
    internal sealed class FakeRuntime : IRuntime
    {
        internal int[] Owners = new int[0]; internal ProcInfo Process; internal IdentityReply Reply;
        internal int Stops, Kills, Launches, IdentityReads; internal Action OnIdentity; internal ProcInfo KilledProcess; internal Identity StoppedIdentity; internal int ActionPort;
        public int[] Listeners(int port) { return Owners; }
        public ProcInfo ReadProcess(int pid) { return Process; }
        public IdentityReply ReadIdentity(int port) { IdentityReads++; if (OnIdentity != null) OnIdentity(); return Reply; }
        public void GracefulStop(int port, Identity identity) { Stops++; StoppedIdentity = identity; ActionPort = port; }
        public void KillExact(ProcInfo process, int port) { Kills++; KilledProcess = process; ActionPort = port; }
        public void Launch(string root, int port, string logs) { Launches++; }
    }
    internal sealed class FakeRunStore : IRunStore
    {
        internal Dictionary<string, string> Entries = new Dictionary<string, string>(); internal int Writes;
        public string Read(string name) { string value; return Entries.TryGetValue(name, out value) ? value : null; }
        public void Set(string name, string value) { Writes++; Entries[name] = value; }
        public void Remove(string name) { Writes++; Entries.Remove(name); }
    }
    internal sealed class FixtureServer : IDisposable
    {
        private TcpListener listener; private readonly Thread worker; private volatile bool stopped;
        internal readonly int Port; internal string Body = "{}"; internal int Status = 200;
        internal int Requests, Posts; internal string LastRequest, LastBody, LastOrigin;
        internal FixtureServer()
        {
            do
            {
                listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
                Port = ((IPEndPoint)listener.LocalEndpoint).Port;
                if (Port == 8000 || Port == 8001) listener.Stop();
            } while (Port == 8000 || Port == 8001);
            worker = new Thread(Serve) { IsBackground = true }; worker.Start();
        }
        private void Serve()
        {
            while (!stopped)
            {
                try
                {
                    using (TcpClient client = listener.AcceptTcpClient())
                    {
                        client.ReceiveTimeout = 3000; client.SendTimeout = 3000;
                        using (NetworkStream stream = client.GetStream())
                        using (StreamReader reader = new StreamReader(stream, Encoding.UTF8, false, 1024, true))
                        {
                            string first = reader.ReadLine(); if (first == null) continue;
                            string line; int length = 0; LastOrigin = null;
                            while (!String.IsNullOrEmpty(line = reader.ReadLine()))
                            {
                                if (line.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase)) length = Int32.Parse(line.Substring(15).Trim());
                                if (line.StartsWith("Origin:", StringComparison.OrdinalIgnoreCase)) LastOrigin = line.Substring(7).Trim();
                            }
                            if (length > 32768) throw new IOException("Fixture body too large.");
                            char[] payload = new char[length]; int read = 0;
                            while (read < length) { int n = reader.Read(payload, read, length - read); if (n == 0) break; read += n; }
                            LastBody = new String(payload, 0, read); LastRequest = first; Requests++; if (first.StartsWith("POST ")) Posts++;
                            byte[] body = Encoding.UTF8.GetBytes(Body);
                            byte[] header = Encoding.ASCII.GetBytes("HTTP/1.1 " + Status + " Fixture\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + body.Length + "\r\n\r\n");
                            stream.Write(header, 0, header.Length); stream.Write(body, 0, body.Length);
                        }
                    }
                }
                catch (SocketException) { if (!stopped) Thread.Sleep(10); }
                catch (IOException) { }
                catch (ObjectDisposedException) { return; }
            }
        }
        public void Dispose() { stopped = true; listener.Stop(); worker.Join(4000); }
    }
    internal static class SelfTests
    {
        private const string Root = @"C:\Fixture Repo\Proxy"; private const string Sid = "S-1-5-21-fixture", Owner = @"FIXTURE\User"; private const int Port = 54321;
        private static readonly List<string> Results = new List<string>(); private static int failed;
        private static void Check(string name, Action test)
        {
            try { test(); Results.Add("PASS  " + name); }
            catch (Exception ex) { failed++; Results.Add("FAIL  " + name + " — " + ex.Message); }
        }
        private static void Assert(bool condition, string message) { if (!condition) throw new Exception(message); }
        private static void Refuses(Action action)
        {
            bool refused = false; try { action(); } catch (InvalidOperationException) { refused = true; }
            Assert(refused, "Unsafe action was not refused.");
        }
        private static FakeRuntime Fixture(bool relative)
        {
            DateTime created = new DateTime(2026, 9, 26, 2, 0, 0, DateTimeKind.Utc);
            string script = relative ? "proxy.py" : Root + @"\proxy.py";
            ProcInfo process = new ProcInfo { Pid = 4242, Executable = Root + @"\.venv\Scripts\python.exe", OwnerSid = Sid, OwnerName = Owner, CreatedUtc = created,
                Args = new string[] { "python.exe", script }, CommandLine = "python.exe " + Paths.Quote(script) };
            Identity identity = new Identity { service = "ghcp_proxy", project_root = Root, pid = 4242, port = Port, instance_id = "fixture-instance-a",
                started_at = (created - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds + 2 };
            return new FakeRuntime { Owners = new int[] { 4242 }, Process = process, Reply = new IdentityReply { Value = identity } };
        }
        private static Controller Control(FakeRuntime runtime) { return new Controller(runtime, Root, Sid, Owner); }
        private static void Command(FakeRuntime runtime, params string[] args)
        {
            runtime.Process.Args = (new string[] { "python.exe" }).Concat(args).ToArray();
            runtime.Process.CommandLine = String.Join(" ", runtime.Process.Args.Select(Paths.Quote));
        }
        private static void NoActions(FakeRuntime runtime)
        { Assert(runtime.Stops + runtime.Kills + runtime.Launches == 0, "Unexpected runtime action"); }
        private static void NewSafetyChecks(string temp)
        {
            string[][] flags = new string[][] {
                new string[] { "-X", "utf8" }, new string[] { "-W", "ignore" },
                new string[] { "-Xutf8" }, new string[] { "-Wignore" },
                new string[] { "-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-X", "dev", "-Werror" }
            };
            foreach (string[] options in flags)
            {
                foreach (bool relative in new bool[] { false, true })
                {
                    Check("Python flags adopted with verified identity: " + String.Join(" ", options) + (relative ? " relative" : " absolute"), delegate {
                        FakeRuntime f = Fixture(relative); Command(f, options.Concat(new string[] { relative ? "proxy.py" : Root + @"\proxy.py" }).ToArray());
                        Controller c = Control(f); Assert(c.Inspect(Port).Kind == ServiceKind.Running, "Flags rejected"); c.Start(Port, temp); NoActions(f);
                    });
                }
                Check("Flagged absolute legacy launch still requires confirmation: " + String.Join(" ", options), delegate {
                    FakeRuntime f = Fixture(false); Command(f, options.Concat(new string[] { Root + @"\proxy.py" }).ToArray()); f.Reply = new IdentityReply { Missing = true };
                    Controller c = Control(f); ServiceState state = c.Inspect(Port); Assert(state.Kind == ServiceKind.Legacy && state.RequiresForceStop, "Legacy flags rejected"); Refuses(delegate { c.Stop(state, false); }); NoActions(f);
                });
            }
            foreach (string[] args in new string[][] { new string[] { "-X" }, new string[] { "-W" }, new string[] { "-X", "utf8" }, new string[] { "-W", "ignore" }, new string[] { "-X", Root + @"\proxy.py" }, new string[] { "-W", Root + @"\proxy.py" }, new string[] { "-c", "proxy.py" }, new string[] { "-Xutf8", "other.py", "proxy.py" }, new string[] { "-m" }, new string[] { "-m", "uvicorn" }, new string[] { "-m", "other", "proxy:app" }, new string[] { "-m", "uvicorn", "other:app" } })
            {
                Check("Malformed or unrelated command refused: " + String.Join(" ", args), delegate {
                    FakeRuntime f = Fixture(false); Command(f, args); Controller c = Control(f); ServiceState state = c.Inspect(Port);
                    Assert(!state.CanStop, "Unsafe command accepted"); Refuses(delegate { c.Start(Port, temp); }); Refuses(delegate { c.Stop(state, true); }); NoActions(f);
                });
            }
            Check("uvicorn module with flags is adopted only with verified identity", delegate {
                FakeRuntime f = Fixture(true); Command(f, "-X", "utf8", "-Wignore", "-m", "uvicorn", "proxy:app", "--port", Port.ToString());
                Controller c = Control(f); Assert(c.Inspect(Port).Kind == ServiceKind.Running, "Verified module rejected"); c.Start(Port, temp); NoActions(f);
            });
            foreach (string invalid in new string[] { "missing", "timeout", "root", "pid", "port", "instance", "owner" })
            {
                Check("uvicorn refuses unverified identity: " + invalid, delegate {
                    FakeRuntime f = Fixture(true); Command(f, "-m", "uvicorn", "proxy:app");
                    switch (invalid) {
                        case "missing": f.Reply = new IdentityReply { Missing = true }; break;
                        case "timeout": f.Reply = new IdentityReply { Error = "timeout" }; break;
                        case "root": f.Reply.Value.project_root = Root + "-other"; break;
                        case "pid": f.Reply.Value.pid++; break;
                        case "port": f.Reply.Value.port++; break;
                        case "instance": f.Reply.Value.instance_id = ""; break;
                        case "owner": f.Process.OwnerSid = "other"; break;
                    }
                    Controller c = Control(f); ServiceState state = c.Inspect(Port); Assert(!state.CanStop && state.Kind != ServiceKind.Legacy, "Module accepted without identity");
                    Refuses(delegate { c.Start(Port, temp); }); Refuses(delegate { c.Stop(state, true); }); NoActions(f);
                });
            }
            foreach (bool? capability in new bool?[] { null, true, false })
            {
                Check("Stop capability policy: " + (capability.HasValue ? capability.Value.ToString() : "omitted"), delegate {
                    FakeRuntime f = Fixture(false); f.Reply.Value.graceful_stop_supported = capability; Controller c = Control(f); ServiceState state = c.Inspect(Port);
                    Assert(state.Kind == ServiceKind.Running && state.RequiresForceStop == (capability == false), "Wrong force policy");
                    if (capability == false) { Refuses(delegate { c.Stop(state, false); }); NoActions(f); c.Stop(state, true); Assert(f.Kills == 1 && f.Stops == 0 && f.KilledProcess.SameProcess(state.Process), "Wrong exact kill"); }
                    else { c.Stop(state, false); Assert(f.Stops == 1 && f.Kills == 0 && f.StoppedIdentity.SameInstance(state.Identity), "Wrong graceful stop"); }
                    Assert(f.ActionPort == Port && f.IdentityReads >= 2, "Stop did not reverify identity/port");
                });
            }
            Check("Verified uvicorn unsupported graceful stop needs force confirmation", delegate {
                FakeRuntime f = Fixture(true); Command(f, "-m", "uvicorn", "proxy:app"); f.Reply.Value.graceful_stop_supported = false;
                Controller c = Control(f); ServiceState state = c.Inspect(Port); Assert(state.RequiresForceStop, "Missing force requirement"); Refuses(delegate { c.Stop(state, false); }); NoActions(f);
                c.Stop(state, true); Assert(f.Kills == 1 && f.Stops == 0 && f.ActionPort == Port, "Wrong module stop path");
            });
            foreach (string change in new string[] { "instance", "capability", "missing", "timeout", "root", "owner", "creation", "command", "listener" })
            {
                Check("Confirmed force stop revalidates and refuses changed " + change, delegate {
                    FakeRuntime f = Fixture(false); f.Reply.Value.graceful_stop_supported = false; Controller c = Control(f); ServiceState original = c.Inspect(Port);
                    f.Process = Fixture(false).Process; f.Reply = Fixture(false).Reply; f.Reply.Value.graceful_stop_supported = false;
                    f.OnIdentity = delegate {
                        switch (change) {
                            case "instance": f.Reply.Value.instance_id = "replacement"; break;
                            case "capability": f.Reply.Value.graceful_stop_supported = true; break;
                            case "missing": f.Reply = new IdentityReply { Missing = true }; break;
                            case "timeout": f.Reply = new IdentityReply { Error = "timeout" }; break;
                            case "root": f.Reply.Value.project_root = Root + "-other"; break;
                            case "owner": f.Process = Fixture(false).Process; f.Process.OwnerSid = "other"; break;
                            case "creation": f.Process = Fixture(false).Process; f.Process.CreatedUtc = f.Process.CreatedUtc.AddSeconds(1); break;
                            case "command": f.Process = Fixture(false).Process; Command(f, "other.py"); break;
                            case "listener": f.Owners = new int[] { 5000 }; break;
                        }
                    };
                    Refuses(delegate { c.Stop(original, true); }); Assert(f.IdentityReads == 2, "Identity was not reread"); NoActions(f);
                });
            }
            Check("Graceful capability changing to false refuses stale confirmed stop", delegate {
                FakeRuntime f = Fixture(false); f.Reply.Value.graceful_stop_supported = true; Controller c = Control(f); ServiceState state = c.Inspect(Port);
                f.Reply = Fixture(false).Reply; f.Reply.Value.graceful_stop_supported = false; Refuses(delegate { c.Stop(state, true); }); NoActions(f);
            });
        }
        private static bool HasPreviewContent(System.Drawing.Bitmap bitmap)
        {
            HashSet<int> colors = new HashSet<int>(); int bright = 0;
            for (int y = 0; y < bitmap.Height; y += 2)
                for (int x = 0; x < bitmap.Width; x += 2)
                {
                    System.Drawing.Color color = bitmap.GetPixel(x, y); colors.Add(color.ToArgb());
                    if (color.R + color.G + color.B > 450) bright++;
                }
            return bitmap.Width >= 300 && bitmap.Height >= 300 && colors.Count >= 16 && bright >= 100;
        }
        public static int Run(string report)
        {
            string temp = Path.Combine(Path.GetTempPath(), "bps-manager-selftest-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(temp);
            try
            {
                NewSafetyChecks(temp);
                Check("External same-project absolute launch is adopted (no start)", delegate { FakeRuntime f = Fixture(false); Controller c = Control(f); Assert(c.Inspect(Port).Kind == ServiceKind.Running, "Not detected"); c.Start(Port, temp); Assert(f.Launches == 0, "Duplicate launched"); });
                Check("External relative launch is adopted with new identity", delegate { FakeRuntime f = Fixture(true); Assert(Control(f).Inspect(Port).Kind == ServiceKind.Running, "Relative identity rejected"); });
                Check("Stopped service launches once through adapter", delegate { FakeRuntime f = Fixture(false); f.Owners = new int[0]; Control(f).Start(Port, temp); Assert(f.Launches == 1, "Missing launch"); });
                Check("Port conflict: non-GHCP identity refuses start and stop", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.service = "other"; Controller c = Control(f); ServiceState s = c.Inspect(Port); Assert(s.Kind == ServiceKind.Conflict, "Not conflict"); Refuses(delegate { c.Start(Port, temp); }); Refuses(delegate { c.Stop(s, true); }); Assert(f.Stops + f.Kills + f.Launches == 0, "Side effect"); });
                Check("Different project root is refused", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.project_root = @"C:\Elsewhere"; Assert(Control(f).Inspect(Port).Kind == ServiceKind.Conflict, "Wrong project accepted"); });
                Check("Prefix-similar project path is not equal", delegate { Assert(!Paths.Same(Root, Root + "-other"), "Prefix accepted"); });
                Check("Case and trailing separator normalize consistently", delegate { Assert(Paths.Same(Root, Root.ToUpperInvariant() + @"\"), "Normalization failed"); Assert(Paths.ProjectKey(Root) == Paths.ProjectKey(Root.ToLowerInvariant()), "Unstable project hash"); });
                Check("Listener PID must match server identity", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.pid++; Assert(!Control(f).Inspect(Port).CanStop, "PID mismatch accepted"); });
                Check("Identity port must match configured port", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.port++; Assert(!Control(f).Inspect(Port).CanStop, "Port mismatch accepted"); });
                Check("Process owner SID must be current user", delegate { FakeRuntime f = Fixture(false); f.Process.OwnerSid = "other"; Assert(!Control(f).Inspect(Port).CanStop, "Owner mismatch accepted"); });
                Check("GetOwner name must match current user", delegate { FakeRuntime f = Fixture(false); f.Process.OwnerName = @"OTHER\User"; Assert(!Control(f).Inspect(Port).CanStop, "Name mismatch accepted"); });
                Check("Non-Python executable is refused", delegate { FakeRuntime f = Fixture(false); f.Process.Executable = @"C:\Windows
otepad.exe"; Assert(!Control(f).Inspect(Port).CanStop, "Wrong executable accepted"); });
                Check("Another Python script with proxy.py argument is refused", delegate { FakeRuntime f = Fixture(false); f.Process.Args = new string[] { "python.exe", "other.py", Root + @"\proxy.py" }; Assert(!Control(f).Inspect(Port).CanStop, "Wrong script accepted"); });
                Check("Python -c cannot masquerade as script path", delegate { FakeRuntime f = Fixture(false); f.Process.Args = new string[] { "python.exe", "-c", Root + @"\proxy.py" }; Assert(!Control(f).Inspect(Port).CanStop, "-c accepted"); });
                Check("Identity startup delta greater than 30s is refused", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.started_at += 60; Assert(!Control(f).Inspect(Port).CanStop, "Old process accepted"); });
                Check("Identity preceding process creation is refused", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.started_at -= 60; Assert(!Control(f).Inspect(Port).CanStop, "Future process accepted"); });
                Check("Empty instance is refused", delegate { FakeRuntime f = Fixture(false); f.Reply.Value.instance_id = ""; Assert(!Control(f).Inspect(Port).CanStop, "Empty accepted"); });
                Check("Multiple bound owners are refused", delegate { FakeRuntime f = Fixture(false); f.Owners = new int[] { 4242, 4343 }; Assert(Control(f).Inspect(Port).Kind == ServiceKind.Conflict, "Multiple owners accepted"); });
                Check("Duplicate IPv4/IPv6 rows for same PID are accepted", delegate { FakeRuntime f = Fixture(false); f.Owners = new int[] { 4242, 4242 }; Assert(Control(f).Inspect(Port).CanStop, "Duplicate rows rejected"); });
                Check("Listener change during HTTP verification is refused", delegate { FakeRuntime f = Fixture(false); f.OnIdentity = delegate { f.Owners = new int[] { 5000 }; }; Assert(!Control(f).Inspect(Port).CanStop, "Race accepted"); });
                Check("Graceful stop exact identity uses no force kill", delegate { FakeRuntime f = Fixture(false); Controller c = Control(f); c.Stop(c.Inspect(Port), false); Assert(f.Stops == 1 && f.Kills == 0, "Incorrect stop path"); });
                Check("Stale instance refuses stop", delegate { FakeRuntime f = Fixture(false); Controller c = Control(f); ServiceState original = c.Inspect(Port); f.Reply = Fixture(false).Reply; f.Reply.Value.instance_id = "restarted"; Refuses(delegate { c.Stop(original, false); }); Assert(f.Stops + f.Kills == 0, "Stopped replacement"); });
                Check("Reused PID with new creation time refuses stop", delegate { FakeRuntime f = Fixture(false); Controller c = Control(f); ServiceState original = c.Inspect(Port); f.Process = Fixture(false).Process; f.Process.CreatedUtc = f.Process.CreatedUtc.AddSeconds(1); Refuses(delegate { c.Stop(original, false); }); Assert(f.Stops + f.Kills == 0, "Killed reused PID"); });
                Check("Legacy absolute-path process requires explicit confirmation", delegate { FakeRuntime f = Fixture(false); f.Reply = new IdentityReply { Missing = true }; Controller c = Control(f); ServiceState s = c.Inspect(Port); Assert(s.Kind == ServiceKind.Legacy, "Legacy undetected"); Refuses(delegate { c.Stop(s, false); }); Assert(f.Kills == 0, "Unconfirmed kill"); c.Stop(s, true); Assert(f.Kills == 1 && f.Stops == 0, "Wrong legacy path"); });
                Check("Legacy relative-path process refuses stop and explains upgrade", delegate { FakeRuntime f = Fixture(true); f.Reply = new IdentityReply { Missing = true }; ServiceState s = Control(f).Inspect(Port); Assert(!s.CanStop && s.Detail.Contains("手动重启升级一次"), "Unsafe old relative detection"); });
                Check("HTTP failure never enables legacy kill fallback", delegate { FakeRuntime f = Fixture(false); f.Reply = new IdentityReply { Error = "timeout" }; Assert(!Control(f).Inspect(Port).CanStop, "Timeout treated as old server"); });
                Check("Legacy PID reuse is refused immediately before adapter kill", delegate { FakeRuntime f = Fixture(false); f.Reply = new IdentityReply { Missing = true }; Controller c = Control(f); ServiceState s = c.Inspect(Port); f.Process = Fixture(false).Process; f.Process.CreatedUtc = f.Process.CreatedUtc.AddSeconds(1); Refuses(delegate { c.Stop(s, true); }); Assert(f.Kills == 0, "Stale legacy killed"); });
                Check("Port bounds include 1 and 65535; reject 0/65536", delegate { Settings.ValidatePort(1); Settings.ValidatePort(65535); int rejected = 0; foreach (int p in new int[] { 0, 65536 }) try { Settings.ValidatePort(p); } catch (ArgumentOutOfRangeException) { rejected++; } Assert(rejected == 2, "Invalid port accepted"); });
                Check("Settings default without write, atomic roundtrip and backup", delegate { string file = Path.Combine(temp, "settings.json"); Settings s = new Settings(file); s.Load(); Assert(s.Port == 8001 && !File.Exists(file), "Default writes"); s.SavePort(1); s.SavePort(65535); Settings next = new Settings(file); next.Load(); Assert(next.Port == 65535 && File.Exists(file + ".bak"), "Persistence failed"); });
                Check("Malformed settings preserved until deliberate edit", delegate { string file = Path.Combine(temp, "bad-settings.json"); File.WriteAllText(file, "bad-json"); Settings s = new Settings(file); s.Load(); Assert(s.Port == 8001 && s.Warning != null && File.ReadAllText(file) == "bad-json", "Corruption overwritten"); });
                Check("Autostart quotes executable/root and escapes trailing slash", delegate { string exe = @"C:\Space Folder\BPS-Manager.exe", root = @"C:\Space Folder\Proxy\"; string[] args = Native.SplitCommand(Paths.AutoStartCommand(exe, root)); Assert(args.SequenceEqual(new string[] { exe, "--project", root, "--autostart" }), "Quoting failed"); });
                Check("Command quoting round-trips embedded quotes and slashes", delegate { foreach (string value in new string[] { "", "space value", "x\"y", @"C:\ends with slash\" }) Assert(Native.SplitCommand("app.exe " + Paths.Quote(value))[1] == value, "Roundtrip failed"); });
                Check("Registry adapter read-only initialization and project isolation", delegate { FakeRunStore store = new FakeRunStore(); AutoStart a = new AutoStart(store, Root, Root + @"\BPS-Manager.exe"); AutoStart b = new AutoStart(store, Root + "-other", Root + @"\BPS-Manager.exe"); Assert(!a.Enabled && store.Writes == 0, "Startup mutated registry"); a.Set(true); Assert(a.Enabled && !b.Enabled, "Not project isolated"); a.Set(false); Assert(!a.Enabled, "Disable failed"); });
                Check("Autostart refuses deletion of externally changed entry", delegate { FakeRunStore store = new FakeRunStore(); AutoStart a = new AutoStart(store, Root, Root + @"\BPS-Manager.exe"); a.Set(true); string key = store.Entries.Keys.First(); store.Entries[key] = "other-app"; Refuses(delegate { a.Set(false); }); Assert(store.Entries[key] == "other-app", "Other entry removed"); });
                Check("Launch environment changes only GHCP_PORT in child copy", delegate { string before = Environment.GetEnvironmentVariable("GHCP_PORT"); Dictionary<string, string> child = HiddenLauncher.ChildEnvironment(54321); Assert(child["GHCP_PORT"] == "54321" && Environment.GetEnvironmentVariable("GHCP_PORT") == before, "Parent env changed"); });
                Check("WMI current process owner and creation timestamp", delegate { using (WindowsIdentity user = WindowsIdentity.GetCurrent()) using (Process current = Process.GetCurrentProcess()) { ProcInfo p = new WindowsRuntime().ReadProcess(current.Id); Assert(p != null && p.OwnerSid == user.User.Value && p.OwnerName.Equals(user.Name, StringComparison.OrdinalIgnoreCase), "WMI owner mismatch"); Assert(Native.CreationTime(current.Handle).Ticks / 10 == p.CreatedUtc.Ticks / 10, "WMI creation precision mismatch"); } });
                Check("Ephemeral fixture: native bound PID, HTTP identity and guarded stop shape", delegate
                {
                    using (FixtureServer server = new FixtureServer())
                    {
                        Assert(server.Port != 8000 && server.Port != 8001, "Forbidden fixture port");
                        Identity id = Fixture(false).Reply.Value; id.port = server.Port;
                        server.Body = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(id);
                        Assert(Native.Listeners(server.Port).Contains(Process.GetCurrentProcess().Id), "TCP owner lookup failed");
                        LoopbackHttp http = new LoopbackHttp(); IdentityReply reply = http.ReadIdentity(server.Port);
                        Assert(reply.Value != null && reply.Value.SameInstance(id), "HTTP identity mismatch: " + reply.Error);
                        server.Body = "{\"stopping\":true}"; server.Status = 202; http.Stop(server.Port, id);
                        Assert(server.Posts == 1 && server.LastRequest.StartsWith("POST /api/desktop/stop ") && server.LastBody.Contains("fixture-instance-a") && server.LastOrigin == "http://127.0.0.1:" + server.Port, "Stop request contract mismatch");
                        server.Status = 404; Assert(http.ReadIdentity(server.Port).Missing, "404 not recognized");
                        server.Status = 500; Assert(!http.ReadIdentity(server.Port).Missing, "500 treated as legacy");
                        server.Status = 302; Assert(http.ReadIdentity(server.Port).Value == null, "Redirect followed");
                        server.Status = 200; server.Body = "not json"; Assert(http.ReadIdentity(server.Port).Value == null, "Malformed accepted");
                    }
                });
                Check("Hidden child survives launcher exit and retains stdout/stderr logs", delegate
                {
                    string dir = Path.Combine(temp, "child"); Directory.CreateDirectory(dir);
                    using (Process launcher = Process.Start(new ProcessStartInfo(Application.ExecutablePath, "--fixture-launcher " + Paths.Quote(dir)) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                    { Assert(launcher.WaitForExit(8000) && launcher.ExitCode == 0, "Fixture launcher failed"); }
                    string file = Path.Combine(dir, "child.log"); string text = "";
                    for (int i = 0; i < 40; i++)
                    { Thread.Sleep(100); if (File.Exists(file)) using (FileStream stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) using (StreamReader reader = new StreamReader(stream)) text = reader.ReadToEnd(); if (text.Contains("fixture-stderr")) break; }
                    Assert(text.Contains("port=54321") && text.Contains("cwd=" + dir) && text.Contains("fixture-after-parent-exit") && text.Contains("fixture-stderr"), "Durable redirection failed: " + text);
                });
                Check("Preview CLI renders real content and rejects a uniform blank bitmap", delegate
                {
                    using (System.Drawing.Bitmap blank = new System.Drawing.Bitmap(460, 494))
                    {
                        using (System.Drawing.Graphics graphics = System.Drawing.Graphics.FromImage(blank)) graphics.Clear(System.Drawing.Color.FromArgb(12, 20, 24));
                        Assert(!HasPreviewContent(blank), "Uniform blank image passed content gate");
                    }
                    string image = Path.Combine(temp, "preview-regression.png");
                    using (Process preview = Process.Start(new ProcessStartInfo(Application.ExecutablePath, "--preview " + Paths.Quote(image)) { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                    { Assert(preview.WaitForExit(15000) && preview.ExitCode == 0, "Preview child failed or timed out"); }
                    Assert(File.Exists(image), "Preview PNG missing");
                    using (System.Drawing.Bitmap bitmap = new System.Drawing.Bitmap(image))
                        Assert(HasPreviewContent(bitmap), "Preview is blank or lacks visible UI content");
                });
                Check("Title bar owns the only X and minimize keeps taskbar without service actions", delegate
                {
                    FakeRuntime f = Fixture(false); Settings settings = new Settings(Path.Combine(temp, "caption-unused.json"));
                    using (ManagerForm form = new ManagerForm(Control(f), settings, null, Root, temp, false, true))
                    {
                        Control[] bars = form.Controls.Find("WindowTitleBar", true);
                        Control[] closes = form.Controls.Find("WindowClose", true);
                        Control[] minimizes = form.Controls.Find("WindowMinimize", true);
                        Assert(bars.Length == 1 && closes.Length == 1 && minimizes.Length == 1, "Caption controls missing or duplicated");
                        Assert(closes[0].Parent == bars[0] && minimizes[0].Parent == bars[0], "Caption actions outside title bar");
                        Assert(bars[0].Top == 12 && bars[0].Height == 36, "Title bar is not at top");
                        form.StartPosition = FormStartPosition.Manual; form.Location = new System.Drawing.Point(-32000, -32000);
                        form.Show(); Application.DoEvents();
                        ((Button)minimizes[0]).PerformClick();
                        Assert(form.WindowState == FormWindowState.Minimized && form.ShowInTaskbar, "Minimize did not preserve taskbar");
                        Assert(!form.ExitCommitted && f.Kills + f.Stops + f.Launches == 0, "Caption touched service");
                        form.WindowState = FormWindowState.Normal; form.Hide();
                    }
                });
                Check("X cancels close, preserves form/tray lifetime, and performs no service action", delegate
                {
                    FakeRuntime f = Fixture(false); Settings s = new Settings(Path.Combine(temp, "unused.json"));
                    using (ManagerForm form = new ManagerForm(Control(f), s, null, Root, temp, false, true))
                    {
                        form.SyntheticState(new ServiceState { Port = Port, Kind = ServiceKind.Running, Detail = "Synthetic" });
                        FormClosingEventArgs close = new FormClosingEventArgs(CloseReason.UserClosing, false); form.SimulateUserClose(close);
                        Assert(close.Cancel && !form.IsDisposed && form.OwnsTray && !form.ExitCommitted && f.Kills + f.Stops + f.Launches == 0, "X ended service or manager lifetime");
                    }
                });
                Check("Exit dialog defaults to leave running and cancel is non-destructive", delegate { using (ExitDialog d = new ExitDialog(true)) { Assert(d.AcceptButton != null && ((Button)d.AcceptButton).Text == "保持服务运行" && d.Choice == ExitChoice.Cancel, "Unsafe exit default"); } });
                Check("Project singleton event activates owner without service actions", delegate
                {
                    string key = "selftest-" + Guid.NewGuid().ToString("N");
                    using (SingleInstance first = new SingleInstance(key))
                    using (ManualResetEvent activated = new ManualResetEvent(false))
                    {
                        Assert(first.IsOwner, "Missing singleton owner"); first.OnActivation(delegate { activated.Set(); });
                        bool duplicateOwner = true; Thread duplicate = new Thread(delegate() { using (SingleInstance second = new SingleInstance(key)) { duplicateOwner = second.IsOwner; second.Signal(); } });
                        duplicate.Start(); Assert(duplicate.Join(3000), "Duplicate blocked"); Assert(!duplicateOwner && activated.WaitOne(3000), "Singleton signal failed");
                    }
                });
            }
            finally
            {
                int total = Results.Count; Results.Add(""); Results.Add(total + " checks recorded; passed: " + (total - failed) + "; failures: " + failed);
                Results.Add("Safety: synthetic controllers/registry; real HTTP only ephemeral loopback !=8000/8001; fixture child is this EXE, never proxy.py.");
                Results.Add("No user manager settings, Run registry values, or actual proxy service were modified.");
                File.WriteAllLines(report, Results.ToArray(), Encoding.UTF8);
                try { Directory.Delete(temp, true); } catch { }
            }
            return failed == 0 ? 0 : 1;
        }
    }
}
