using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace BpsManager
{
    internal static class Theme
    {
        public static readonly Color Background = Color.FromArgb(12, 20, 24), Card = Color.FromArgb(22, 33, 38),
            Mint = Color.FromArgb(134, 237, 196), Text = Color.FromArgb(235, 245, 240),
            Muted = Color.FromArgb(147, 166, 163), Line = Color.FromArgb(47, 64, 67), Warning = Color.FromArgb(255, 200, 132);
    }
    internal sealed class CardPanel : Panel
    {
        public CardPanel() { DoubleBuffered = true; BackColor = Theme.Card; }
        protected override void OnPaint(PaintEventArgs e)
        { base.OnPaint(e); using (Pen p = new Pen(Theme.Line)) e.Graphics.DrawRectangle(p, 0, 0, Width - 1, Height - 1); }
    }
    internal sealed class ActionButton : Button
    {
        public bool Primary;
        public ActionButton() { FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; Cursor = Cursors.Hand; Font = new Font("Segoe UI", 10, FontStyle.Bold); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(Enabled ? (Primary ? Theme.Mint : Theme.Line) : Color.FromArgb(30, 43, 46));
            TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, Enabled ? (Primary ? Theme.Background : Theme.Text) : Theme.Muted, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
            if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, new Rectangle(4, 4, Width - 8, Height - 8));
        }
    }
    internal enum ExitChoice { Cancel, LeaveRunning, StopService }
    internal sealed class ExitDialog : Form
    {
        public ExitChoice Choice = ExitChoice.Cancel;
        public ExitDialog(bool canStop)
        {
            Text = "退出 BPS 代理管理器"; ClientSize = new Size(426, 168); FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; MinimizeBox = false; StartPosition = FormStartPosition.CenterParent; BackColor = Theme.Card; ForeColor = Theme.Text; Font = new Font("Segoe UI", 9);
            Controls.Add(new Label { Text = "退出管理器后，如何处理代理服务？\n默认保持服务运行，避免中断当前请求。", Location = new Point(20, 20), Size = new Size(390, 45) });
            Button leave = new ActionButton { Primary = true, Text = "保持服务运行", Location = new Point(20, 87), Size = new Size(135, 36) };
            Button stop = new ActionButton { Text = "停止并退出", Location = new Point(164, 87), Size = new Size(130, 36), Enabled = canStop };
            Button cancel = new ActionButton { Text = "取消", Location = new Point(303, 87), Size = new Size(102, 36) };
            leave.Click += delegate { Choice = ExitChoice.LeaveRunning; DialogResult = DialogResult.OK; };
            stop.Click += delegate { Choice = ExitChoice.StopService; DialogResult = DialogResult.OK; };
            cancel.Click += delegate { DialogResult = DialogResult.Cancel; };
            Controls.AddRange(new Control[] { leave, stop, cancel }); AcceptButton = leave; CancelButton = cancel;
        }
    }
    internal sealed class ManagerForm : Form
    {
        private readonly Controller controller; private readonly Settings settings; private readonly AutoStart autoStart;
        private readonly string root, logDirectory; private readonly bool preview, startMinimized;
        private readonly Label stateText, detail, footer, project; private readonly Panel light;
        private readonly NumericUpDown port; private readonly Button start, stop, open; private readonly CheckBox logon;
        private readonly NotifyIcon tray; private readonly Timer timer;
        private readonly ToolStripMenuItem trayStart, trayStop, trayOpen;
        private ServiceState state; private bool closing, busy, monitoring, loading = true, ready;
        private int revision; private DateTime startDeadline = DateTime.MinValue; private bool stopping;
        [DllImport("user32.dll")] private static extern bool ReleaseCapture();
        [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
        public ManagerForm(Controller controller, Settings settings, AutoStart autoStart, string root, string logs, bool minimized, bool preview)
        {
            this.controller = controller; this.settings = settings; this.autoStart = autoStart; this.root = root;
            logDirectory = logs; startMinimized = minimized; this.preview = preview;
            AutoScaleMode = AutoScaleMode.Dpi; ClientSize = new Size(460, 530); FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.CenterScreen; BackColor = Theme.Background; ForeColor = Theme.Text;
            Font = new Font("Segoe UI", 9); Text = "BPS 代理"; DoubleBuffered = true;
            Icon = MakeIcon();
            Panel titleBar = new Panel { Name = "WindowTitleBar", Location = new Point(12, 12), Size = new Size(436, 36), BackColor = Theme.Card, AccessibleName = "可拖动标题栏" };
            Controls.Add(titleBar);
            Label caption = Label(titleBar, "BPS 代理管理器", 12, 9, 290, 22, 9, Theme.Muted, false);
            foreach (Control surface in new Control[] { titleBar, caption })
                surface.MouseDown += delegate(object sender, MouseEventArgs e) { if (e.Button == MouseButtons.Left) { ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero); } };
            Button minimize = CaptionButton(titleBar, "WindowMinimize", "—", 348, "最小化到任务栏");
            minimize.Click += delegate { ShowInTaskbar = true; WindowState = FormWindowState.Minimized; };
            Button close = CaptionButton(titleBar, "WindowClose", "×", 392, "关闭窗口并收到系统托盘");
            close.FlatAppearance.MouseOverBackColor = Color.FromArgb(150, 54, 65);
            close.Click += delegate { Close(); };
            CardPanel card = new CardPanel { Location = new Point(12, 48), Size = new Size(436, 470) }; Controls.Add(card);
            Label badge = Label(card, "B", 22, 18, 34, 38, 23, Theme.Mint, true);
            Label title = Label(card, "BPS 代理", 64, 22, 255, 29, 17, Theme.Text, true);
            Label subtitle = Label(card, "本地服务 · 桌面管理", 24, 64, 358, 20, 8, Theme.Muted, false);
            foreach (Control c in new Control[] { card, badge, title, subtitle }) c.MouseDown += delegate(object sender, MouseEventArgs e) { if (e.Button == MouseButtons.Left) { ReleaseCapture(); SendMessage(Handle, 0xA1, new IntPtr(2), IntPtr.Zero); } };
            light = new Panel { Location = new Point(25, 112), Size = new Size(9, 9), BackColor = Theme.Muted }; card.Controls.Add(light);
            stateText = Label(card, "正在检查服务", 45, 103, 350, 30, 16, Theme.Text, true);
            detail = Label(card, "正在核对本地服务身份和进程归属…", 24, 143, 388, 49, 9, Theme.Muted, false);
            Panel line = new Panel { Location = new Point(24, 200), Size = new Size(388, 1), BackColor = Theme.Line }; card.Controls.Add(line);
            Label(card, "监听端口", 24, 219, 150, 20, 8, Theme.Muted, true);
            port = new NumericUpDown { Location = new Point(276, 213), Size = new Size(136, 30), Minimum = 1, Maximum = 65535, Value = settings.Port, BackColor = Theme.Background, ForeColor = Theme.Text, BorderStyle = BorderStyle.FixedSingle, Font = new Font("Segoe UI", 12), TextAlign = HorizontalAlignment.Right, AccessibleName = "服务端口" }; card.Controls.Add(port);
            port.ValueChanged += PortChanged;
            open = Button(card, "打开管理页面   ↗", 24, 266, 388, 42, true); open.Click += delegate { OpenPage(); };
            start = Button(card, "启动服务", 24, 319, 188, 38, false); start.Click += async delegate { await StartService(); };
            stop = Button(card, "停止服务", 224, 319, 188, 38, false); stop.Click += async delegate { await StopService(); };
            logon = new CheckBox { Text = "登录 Windows 时自动启动", Location = new Point(24, 377), Size = new Size(388, 24), ForeColor = Theme.Text, FlatStyle = FlatStyle.Flat, AccessibleName = "当前用户登录系统时自动启动" }; card.Controls.Add(logon);
            try { logon.Checked = autoStart != null && autoStart.Enabled; } catch (Exception ex) { settings.Warning = "无法读取自启动设置：" + ex.Message; }
            logon.CheckedChanged += LogonChanged;
            project = Label(card, System.IO.Path.GetFileName(root), 24, 413, 388, 19, 8, Theme.Muted, false); project.AutoEllipsis = true;
            footer = Label(card, "关闭窗口收到托盘 · 设置按项目保存", 24, 438, 388, 18, 8, Theme.Muted, false);
            ContextMenuStrip menu = new ContextMenuStrip { BackColor = Theme.Card, ForeColor = Theme.Text, ShowImageMargin = false };
            menu.Items.Add("显示管理器", null, delegate { ShowManager(); });
            trayOpen = (ToolStripMenuItem)menu.Items.Add("打开管理页面", null, delegate { OpenPage(); });
            menu.Items.Add(new ToolStripSeparator());
            trayStart = (ToolStripMenuItem)menu.Items.Add("启动服务", null, async delegate { await StartService(); });
            trayStop = (ToolStripMenuItem)menu.Items.Add("停止服务", null, async delegate { ShowManager(); await StopService(); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出…", null, async delegate { ShowManager(); await RequestExit(); });
            tray = new NotifyIcon { Text = "BPS 代理", Icon = Icon, ContextMenuStrip = menu, Visible = !preview };
            tray.DoubleClick += delegate { ShowManager(); };
            timer = new Timer { Interval = 2500 }; timer.Tick += async delegate { await RefreshState(); };
            Shown += async delegate
            {
                if (preview) return;
                if (startMinimized) { Hide(); ShowInTaskbar = false; }
                await RefreshState(); timer.Start();
                if (startMinimized) await StartService();
                else if (settings.Warning != null) ShowError(settings.Warning);
            };

            loading = false; ApplyState(new ServiceState { Port = settings.Port, Kind = ServiceKind.Unverified, Detail = "正在核对本地服务身份和进程归属…" });
        }
        private static Button CaptionButton(Control parent, string name, string text, int x, string accessibleName)
        {
            Button button = new Button { Name = name, Text = text, Location = new Point(x, 0), Size = new Size(44, 36), FlatStyle = FlatStyle.Flat, ForeColor = Theme.Text, BackColor = Theme.Card, Font = new Font("Segoe UI", 12), Cursor = Cursors.Hand, AccessibleName = accessibleName };
            button.FlatAppearance.BorderSize = 0;
            button.FlatAppearance.MouseOverBackColor = Theme.Line;
            button.FlatAppearance.MouseDownBackColor = Theme.Background;
            parent.Controls.Add(button); return button;
        }
        private static Label Label(Control parent, string text, int x, int y, int w, int h, float size, Color color, bool bold)
        {
            Label label = new Label { Text = text, Location = new Point(x, y), Size = new Size(w, h), ForeColor = color, BackColor = Color.Transparent, Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular) }; parent.Controls.Add(label); return label;
        }
        private static Button Button(Control parent, string text, int x, int y, int w, int h, bool primary)
        { ActionButton button = new ActionButton { Text = text, Location = new Point(x, y), Size = new Size(w, h), Primary = primary }; parent.Controls.Add(button); return button; }
        private static Icon MakeIcon()
        {
            using (Bitmap b = new Bitmap(32, 32))
            using (Graphics g = Graphics.FromImage(b))
            using (Font font = new Font("Segoe UI", 20, FontStyle.Bold))
            {
                g.Clear(Theme.Background); TextRenderer.DrawText(g, "B", font, new Rectangle(0, 0, 32, 32), Theme.Mint, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                IntPtr handle = b.GetHicon(); try { return (Icon)Icon.FromHandle(handle).Clone(); } finally { DestroyIcon(handle); }
            }
        }
        [DllImport("user32.dll")] private static extern bool DestroyIcon(IntPtr icon);
        public void ShowManager() { if (closing || IsDisposed) return; ShowInTaskbar = true; Show(); WindowState = FormWindowState.Normal; Activate(); }
        private void HideToTray() { if (preview) return; Hide(); ShowInTaskbar = false; }
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (!closing && e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; HideToTray(); }
            base.OnFormClosing(e);
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing) { if (timer != null) { timer.Stop(); timer.Dispose(); } if (tray != null) { tray.Visible = false; tray.ContextMenuStrip.Dispose(); tray.Dispose(); } if (Icon != null) Icon.Dispose(); }
            base.Dispose(disposing);
        }
        private async Task RefreshState()
        {
            if (preview || monitoring || busy || closing) return;
            monitoring = true; int stamp = revision; int selected = settings.Port;
            try
            {
                ServiceState next = await Task.Run(delegate { return controller.Inspect(selected); });
                if (closing || IsDisposed || stamp != revision || busy) return;
                ready = true;
                if (startDeadline > DateTime.UtcNow && next.Kind == ServiceKind.Stopped)
                    next = new ServiceState { Port = selected, Kind = ServiceKind.Starting, Detail = "等待服务就绪，启动日志保存在用户状态目录。" };
                else if (startDeadline != DateTime.MinValue && next.Kind == ServiceKind.Stopped)
                { startDeadline = DateTime.MinValue; next.Detail = "服务启动尚未就绪，请检查日志、依赖和配置。"; }
                else if (next.Kind == ServiceKind.Running || next.Kind == ServiceKind.Legacy) startDeadline = DateTime.MinValue;
                if (stopping && next.Kind == ServiceKind.Stopped) stopping = false;
                ApplyState(next);
            }
            finally { monitoring = false; }
        }
        private void ApplyState(ServiceState next)
        {
            state = next;
            string label = next.Kind == ServiceKind.Running ? "服务运行中" : next.Kind == ServiceKind.Legacy ? "已识别旧版服务" :
                next.Kind == ServiceKind.Stopped ? "服务已停止" : next.Kind == ServiceKind.Conflict ? "端口被占用 · 已保护" : next.Kind == ServiceKind.Starting ? "正在启动服务…" : "服务尚未确认";
            if (stopping && next.Kind == ServiceKind.Running) label = "正在等待服务退出…";
            stateText.Text = label; detail.Text = next.Detail; light.BackColor = next.Kind == ServiceKind.Running ? Theme.Mint : next.Kind == ServiceKind.Stopped ? Theme.Muted : Theme.Warning;
            tray.Text = "BPS 代理 · " + settings.Port + " · " + label;
            bool idle = !busy && ready;
            start.Enabled = trayStart.Enabled = idle && next.Kind == ServiceKind.Stopped;
            stop.Enabled = trayStop.Enabled = idle && next.CanStop;
            open.Enabled = trayOpen.Enabled = idle && next.CanStop;
            port.Enabled = !busy && next.Kind != ServiceKind.Running && next.Kind != ServiceKind.Legacy && next.Kind != ServiceKind.Starting;
        }
        private void PortChanged(object sender, EventArgs e)
        {
            if (loading || preview) return;
            try
            {
                settings.SavePort((int)port.Value); revision++; ready = false; stopping = false; startDeadline = DateTime.MinValue;
                ApplyState(new ServiceState { Port = settings.Port, Kind = ServiceKind.Unverified, Detail = "正在检查所选端口…" });
                if (!monitoring) { Task ignored = RefreshState(); }
            }
            catch (Exception ex) { loading = true; port.Value = settings.Port; loading = false; ShowError(ex.Message); }
        }
        private void LogonChanged(object sender, EventArgs e)
        {
            if (loading || preview) return;
            try
            {
                if (autoStart.HasDifferentCommand && logon.Checked && MessageBox.Show(this, "本项目已有其他登录启动项，是否替换为此管理器？", "确认修改自启动", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) != DialogResult.Yes)
                { loading = true; logon.Checked = false; loading = false; return; }
                autoStart.Set(logon.Checked);
            }
            catch (Exception ex) { loading = true; logon.Checked = !logon.Checked; loading = false; ShowError(ex.Message); }
        }
        private void OpenPage()
        {
            if (!ready || busy || state == null || !state.CanStop) return;
            try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("http://127.0.0.1:" + settings.Port + "/ui") { UseShellExecute = true }); }
            catch (Exception ex) { ShowError(ex.Message); }
        }
        private async Task StartService()
        {
            if (preview || busy || closing) return;
            busy = true; revision++; ApplyState(state);
            try
            {
                int selected = settings.Port;
                await Task.Run(delegate { controller.Start(selected, logDirectory); });
                startDeadline = DateTime.UtcNow.AddSeconds(25);
            }
            catch (Exception ex) { ShowError(ex.Message); }
            finally { busy = false; if (!closing && !IsDisposed) { ApplyState(state); Task refresh = RefreshState(); } }
        }
        private async Task<bool> StopService()
        {
            if (preview || busy || closing || state == null || !state.CanStop) return false;
            ServiceState expected = state; bool legacy = expected.RequiresForceStop;
            if (legacy && MessageBox.Show(this, "此服务不支持平滑退出。\n\n是否强制停止已验证属于本项目的进程？\nPID " + expected.Process.Pid + "\n进行中的请求可能中断。", "确认停止服务", MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return false;
            busy = true; revision++; ApplyState(state);
            try
            {
                await Task.Run(delegate { controller.Stop(expected, legacy); });
                stopping = true; return true;
            }
            catch (Exception ex) { ShowError(ex.Message); return false; }
            finally { busy = false; if (!closing && !IsDisposed) { ApplyState(state); Task refresh = RefreshState(); } }
        }
        private async Task RequestExit()
        {
            if (busy) { ShowError("操作正在进行，请完成后再退出。"); return; }
            using (ExitDialog dialog = new ExitDialog(state != null && state.CanStop))
            {
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                if (dialog.Choice == ExitChoice.StopService && !await StopService()) return;
            }
            closing = true; timer.Stop(); tray.Visible = false; Close();
        }
        private void ShowError(string message)
        {
            if (closing || IsDisposed) return; detail.Text = message;
            if (Visible) MessageBox.Show(this, message, "BPS 代理", MessageBoxButtons.OK, MessageBoxIcon.Information);
            else { tray.BalloonTipTitle = "BPS 代理"; tray.BalloonTipText = message.Length > 240 ? message.Substring(0, 240) : message; tray.ShowBalloonTip(6000); }
        }
        internal void SyntheticState(ServiceState value) { ready = true; ApplyState(value); }
        internal void SimulateUserClose(FormClosingEventArgs args) { OnFormClosing(args); }
        internal bool OwnsTray { get { return tray != null; } }
        internal bool ExitCommitted { get { return closing; } }
    }
}
