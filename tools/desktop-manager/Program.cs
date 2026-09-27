using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Security.Principal;
using System.Threading;
using System.Windows.Forms;

namespace BpsManager
{
    internal sealed class SingleInstance : IDisposable
    {
        private readonly Mutex mutex; private readonly EventWaitHandle activation; private bool owned;
        private RegisteredWaitHandle listener;
        public SingleInstance(string key)
        {
            activation = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\BPSManager-activate-" + key);
            mutex = new Mutex(false, @"Local\BPSManager-mutex-" + key);
            try { owned = mutex.WaitOne(0, false); } catch (AbandonedMutexException) { owned = true; }
        }
        public bool IsOwner { get { return owned; } }
        public void Signal() { activation.Set(); }
        public void OnActivation(Action action)
        { listener = ThreadPool.RegisterWaitForSingleObject(activation, delegate { action(); }, null, Timeout.Infinite, false); }
        public void Dispose()
        {
            if (listener != null) listener.Unregister(null);
            if (owned) { mutex.ReleaseMutex(); owned = false; }
            activation.Dispose(); mutex.Dispose();
        }
    }
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                // These modes run before user settings, registry access, monitoring, singleton or live UI.
                if (args.Length == 2 && args[0] == "--self-test") return SelfTests.Run(Path.GetFullPath(args[1]));
                if (args.Length == 2 && args[0] == "--preview") return Preview(Path.GetFullPath(args[1]));
                if (args.Length == 2 && args[0] == "--fixture-child")
                {
                    Console.WriteLine("fixture-start;port=" + Environment.GetEnvironmentVariable("GHCP_PORT") + ";cwd=" + Environment.CurrentDirectory);
                    Thread.Sleep(900); Console.WriteLine("fixture-after-parent-exit"); Console.Error.WriteLine("fixture-stderr"); return 0;
                }
                if (args.Length == 2 && args[0] == "--fixture-launcher")
                {
                    string directory = Path.GetFullPath(args[1]);
                    int pid = HiddenLauncher.Launch(Application.ExecutablePath, "--fixture-child test", directory, 54321, Path.Combine(directory, "child.log"));
                    File.WriteAllText(Path.Combine(directory, "child.pid"), pid.ToString()); return 0;
                }
                string root = AppDomain.CurrentDomain.BaseDirectory; bool autostart = false;
                for (int i = 0; i < args.Length; i++)
                {
                    if (args[i] == "--autostart") autostart = true;
                    else if (args[i] == "--project" && i + 1 < args.Length) root = args[++i];
                    else throw new ArgumentException("Unknown option. Supported: --project <absolute root>, --autostart, --self-test <report>, --preview <png>.");
                }
                root = Paths.Normalize(root);
                if (!File.Exists(Path.Combine(root, "proxy.py"))) throw new FileNotFoundException("请将 BPS-Manager.exe 放在项目根目录，当前未找到 proxy.py。");
                using (WindowsIdentity user = WindowsIdentity.GetCurrent())
                using (SingleInstance instance = new SingleInstance(user.User.Value + "-" + Paths.ProjectKey(root)))
                {
                    if (!instance.IsOwner) { if (!autostart) instance.Signal(); return 0; }
                    string stateRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BPS-Manager", Paths.ProjectKey(root));
                    Settings settings = new Settings(Path.Combine(stateRoot, "settings.json")); settings.Load();
                    AutoStart logon = new AutoStart(new RegistryRunStore(), root, Application.ExecutablePath);
                    Controller controller = new Controller(new WindowsRuntime(), root, user.User.Value, user.Name);
                    using (ManagerForm form = new ManagerForm(controller, settings, logon, root, Path.Combine(stateRoot, "logs"), autostart, false))
                    {
                        IntPtr handle = form.Handle;
                        instance.OnActivation(delegate
                        {
                            try { if (!form.IsDisposed && form.IsHandleCreated) form.BeginInvoke(new Action(form.ShowManager)); }
                            catch (InvalidOperationException) { }
                        });
                        if (autostart) form.Opacity = 0;
                        form.Shown += delegate { if (autostart) form.Opacity = 1; };
                        Application.Run(form);
                    }
                }
                return 0;
            }
            catch (Exception ex)
            {
                // Automated modes never display modal UI or fall through to live service control.
                if (args.Length > 0 && (args[0] == "--self-test" || args[0] == "--preview" || args[0].StartsWith("--fixture-")))
                {
                    if (args.Length > 1) try { File.WriteAllText(args[1] + ".error.txt", ex.ToString()); } catch { }
                    return 1;
                }
                MessageBox.Show(ex.Message, "BPS 代理", MessageBoxButtons.OK, MessageBoxIcon.Error); return 1;
            }
        }
        private static int Preview(string output)
        {
            Settings settings = new Settings(Path.Combine(Path.GetTempPath(), "bps-preview-unused.json")); settings.Port = 8001;
            using (ManagerForm form = new ManagerForm(null, settings, null, @"C:\Projects\ghcp_proxy", "", false, true))
            {
                form.SyntheticState(new ServiceState { Port = 8001, Kind = ServiceKind.Running, Detail = "已验证本项目 · 当前用户 · PID 24680" });
                // WinForms child controls must be realized and visible for DrawToBitmap.
                // This preview-only form is offscreen, has no tray, timer, controller or registry writes.
                form.StartPosition = FormStartPosition.Manual;
                form.Location = new Point(-32000, -32000);
                form.ShowInTaskbar = false;
                form.Show();
                Application.DoEvents();
                form.PerformLayout();
                form.Update();
                using (Bitmap bitmap = new Bitmap(form.Width, form.Height))
                { form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, bitmap.Size)); bitmap.Save(output, ImageFormat.Png); }
                form.Hide();
            }
            return 0;
        }
    }
}
