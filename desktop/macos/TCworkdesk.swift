import AppKit
import WebKit

struct BackendConnection: Decodable, Sendable {
    let workspace: String
    let url: String
    let managed: Bool
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var status: NSTextField!
    private var spinner: NSProgressIndicator!
    private var connection: BackendConnection?
    private var workspace: URL?
    private var working = false
    private var preferences = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("TCworkdesk/desktop.json")

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = ProcessInfo.processInfo.arguments
        if let index = args.firstIndex(of: "--preferences"), args.count > index + 1 {
            preferences = URL(fileURLWithPath: args[index + 1])
        }
        if let index = args.firstIndex(of: "--workspace"), args.count > index + 1 {
            workspace = URL(fileURLWithPath: args[index + 1])
        } else if let saved = UserDefaults.standard.string(forKey: "ExternalWorkspace") {
            workspace = URL(fileURLWithPath: saved)
        }
        buildMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "TCworkdesk"
        window.minSize = NSSize(width: 760, height: 540)
        window.setFrameAutosaveName("TCworkdesk.MainWindow")
        window.center()
        window.isReleasedWhenClosed = false
        let content = NSView()
        webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(webView)
        status = NSTextField(labelWithString: "正在准备工作台…")
        status.alignment = .center
        status.font = NSFont.systemFont(ofSize: 15)
        status.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(status)
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(spinner)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: content.topAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            status.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            spinner.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            spinner.bottomAnchor.constraint(equalTo: status.topAnchor, constant: -16)
        ])
        window.contentView = content
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        start()
    }

    private func buildMenu() {
        let bar = NSMenu()
        let app = NSMenuItem()
        bar.addItem(app)
        let menu = NSMenu()
        menu.addItem(withTitle: "关于 TCworkdesk", action: #selector(about), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "数据位置与迁移…", action: #selector(dataSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "连接已有工作台…", action: #selector(chooseWorkspace), keyEquivalent: "o")
        menu.addItem(withTitle: "使用应用独立工作台", action: #selector(useManagedWorkspace), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "隐藏 TCworkdesk", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        menu.addItem(withTitle: "退出 TCworkdesk", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        app.submenu = menu
        let edit = NSMenuItem()
        edit.title = "编辑"
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = editMenu
        bar.addItem(edit)
        let view = NSMenuItem()
        view.title = "窗口"
        let viewMenu = NSMenu(title: "窗口")
        viewMenu.addItem(withTitle: "显示工作台", action: #selector(showWindow), keyEquivalent: "1")
        viewMenu.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        view.submenu = viewMenu
        bar.addItem(view)
        NSApp.mainMenu = bar
    }

    private func runBackend(_ arguments: [String]) async throws -> BackendConnection {
        guard let resource = Bundle.main.resourceURL else { throw CocoaError(.fileNoSuchFile) }
        let executable = resource.appendingPathComponent("backend/TCBackend")
        return try await Task.detached {
            let process = Process()
            let output = Pipe()
            process.executableURL = executable
            process.arguments = ["--desktop"] + arguments
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            process.standardInput = FileHandle.nullDevice
            try process.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            if process.terminationStatus != 0 {
                let object = try? JSONSerialization.jsonObject(with: data) as? [String: String]
                throw NSError(domain: "TCworkdesk", code: Int(process.terminationStatus),
                    userInfo: [NSLocalizedDescriptionKey: object?["error"] ?? "后台操作未完成，原数据已保留。"])
            }
            return try JSONDecoder().decode(BackendConnection.self, from: data)
        }.value
    }

    private func start(action: String = "connect", destination: URL? = nil) {
        guard !working else { return }
        working = true
        status.stringValue = action == "migrate" ? "正在校验并迁移数据，请勿退出…" : "正在连接工作台…"
        status.isHidden = false
        spinner.isHidden = false
        spinner.startAnimation(nil)
        webView.isHidden = true
        var args = [action, "--preferences", preferences.path]
        if let workspace { args += ["--workspace", workspace.path] }
        if let destination { args += ["--destination", destination.path] }
        Task {
            defer { working = false; spinner.stopAnimation(nil); spinner.isHidden = true }
            do {
                let result = try await runBackend(args)
                guard let url = URL(string: result.url + "/?host=desktop"), url.host == "127.0.0.1" else {
                    throw CocoaError(.coderReadCorrupt)
                }
                connection = result
                webView.load(URLRequest(url: url))
                webView.isHidden = false
                status.isHidden = true
                window.subtitle = result.managed ? "独立工作台" : "已连接现有工作台"
            } catch {
                connection = nil
                status.stringValue = "连接未完成，可从菜单重新连接。"
                showError(error.localizedDescription)
            }
        }
    }

    @objc private func dataSettings() {
        guard !working else { return }
        let alert = NSAlert()
        alert.messageText = "数据位置"
        let path = connection.map { $0.managed ? URL(fileURLWithPath: $0.workspace).deletingLastPathComponent().path : $0.workspace } ?? "尚未连接"
        alert.informativeText = "当前安装：\(path)\n\n默认在系统盘用户的 Library/Application Support/TCworkdesk/Data。迁移应用设置、模型配置、缓存及全局运行数据；项目源码与项目 .model-desk 历史随项目保留。系统钥匙串和第三方 CLI 账户不搬移。迁移后保留原目录作为备份。"
        alert.addButton(withTitle: "选择位置并迁移…")
        alert.addButton(withTitle: "在 Finder 中显示")
        alert.addButton(withTitle: "关闭")
        let result = alert.runModal()
        if result == .alertSecondButtonReturn {
            if let connection { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: connection.workspace) }
            return
        }
        guard result == .alertFirstButtonReturn else { return }
        guard connection?.managed == true, workspace == nil else {
            showError("当前连接的是已有工作台。其数据由原安装管理；请先从菜单选择‘使用应用独立工作台’。")
            return
        }
        let panel = NSOpenPanel()
        panel.title = "选择新数据目录的上级文件夹"
        panel.message = "将在所选位置新建 TCworkdesk Data；不覆盖已有目录。请先暂停目标并结束任务。"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let parent = panel.url {
            start(action: "migrate", destination: parent.appendingPathComponent("TCworkdesk Data"))
        }
    }

    @objc private func chooseWorkspace() {
        guard !working else { return }
        let panel = NSOpenPanel()
        panel.title = "选择已有 TCworkdesk／AAD 安装目录"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            workspace = url
            UserDefaults.standard.set(url.path, forKey: "ExternalWorkspace")
            start()
        }
    }
    @objc private func useManagedWorkspace() {
        guard !working else { return }
        workspace = nil
        UserDefaults.standard.removeObject(forKey: "ExternalWorkspace")
        start()
    }
    @objc private func reload() { if connection != nil { webView.reload() } else { start() } }
    @objc private func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc private func about() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "TCworkdesk", .applicationVersion: "0.3.0",
            .credits: NSAttributedString(string: "原生 macOS 窗口 · 本地多智能体工作台\n界面关闭不会中断后台任务。")])
    }
    private func showError(_ message: String) { let alert = NSAlert(); alert.messageText = "操作未完成"; alert.informativeText = message; alert.runModal() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if working { showError("正在准备或迁移数据，请等待操作结束。"); return .terminateCancel }
        // Window ownership is separate from execution ownership. Quitting never kills model work.
        return .terminateNow
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if let base = connection.flatMap({ URL(string: $0.url) }),
           url.scheme == base.scheme, url.host == base.host, url.port == base.port {
            decisionHandler(.allow)
        } else {
            if navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        return nil
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "确定"); alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.runModal(); completionHandler()
    }
}

@main
struct TCworkdeskApp {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
        withExtendedLifetime(delegate) {}
    }
}
