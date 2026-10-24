import AppKit
import WebKit

struct BackendConnection: Codable, Sendable {
    let workspace: String
    let url: String
    let managed: Bool
    let product: String
    let protocolVersion: Int
    let pid: Int
    let birth: Double
    enum CodingKeys: String, CodingKey {
        case workspace, url, managed, product, pid, birth
        case protocolVersion = "protocol"
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var status: NSTextField!
    private var spinner: NSProgressIndicator!
    private var connection: BackendConnection?
    private var workspace: URL?
    private var ownsConnection: Bool { connection?.managed == true && workspace == nil }
    private var working = false
    private var quitting = false
    private var statusItem: NSStatusItem!
    private var retryButton: NSButton!
    private var detailLabel: NSTextField!
    private var failureButtons: NSStackView!
    private var powerOffRequested = false
    private let serviceState = NSMenuItem(title: "正在连接工作台…", action: nil, keyEquivalent: "")
    private var preferences = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("TCmodeldesk/desktop.json")

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
        if !UserDefaults.standard.bool(forKey: "LegacySettingsImported") {
            if workspace == nil,
               let legacy = UserDefaults.standard.persistentDomain(forName: "local.tcworkdesk.desktop")?["ExternalWorkspace"] as? String {
                workspace = URL(fileURLWithPath: legacy)
                UserDefaults.standard.set(legacy, forKey: "ExternalWorkspace")
            }
            UserDefaults.standard.set(true, forKey: "LegacySettingsImported")
        }
        buildMenu()
        buildStatusItem()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "TCmodeldesk"
        window.minSize = NSSize(width: 760, height: 540)
        window.setFrameAutosaveName("TCmodeldesk.MainWindow")
        window.center()
        window.isReleasedWhenClosed = false
        window.delegate = self
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
        detailLabel = NSTextField(wrappingLabelWithString: "")
        detailLabel.alignment = .center
        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.isHidden = true
        detailLabel.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(detailLabel)
        retryButton = NSButton(title: "重新连接", target: self, action: #selector(retryConnection))
        retryButton.bezelStyle = .rounded
        retryButton.keyEquivalent = "\r"
        let existing = NSButton(title: "连接已有工作台…", target: self, action: #selector(chooseWorkspace))
        existing.bezelStyle = .rounded
        let dataFolder = NSButton(title: "数据位置与迁移…", target: self, action: #selector(dataSettings))
        dataFolder.bezelStyle = .rounded
        failureButtons = NSStackView(views: [retryButton, existing, dataFolder])
        failureButtons.orientation = .horizontal
        failureButtons.spacing = 10
        failureButtons.isHidden = true
        failureButtons.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(failureButtons)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: content.topAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            status.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            spinner.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            spinner.bottomAnchor.constraint(equalTo: status.topAnchor, constant: -16),
            detailLabel.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            detailLabel.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 8),
            detailLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            failureButtons.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            failureButtons.topAnchor.constraint(equalTo: detailLabel.bottomAnchor, constant: 18)
        ])
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.powerOffRequested = true }
        }
        window.contentView = content
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        start()
    }

    /// Template glyph (decision node handing work to two execution nodes). Drawn in code so the
    /// menu bar tints it for light, dark and highlighted states; mirrors desktop/assets/tcmodeldesk-menubar.svg.
    private static func menuBarGlyph() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.set()
            let top = NSPoint(x: 9, y: 13.8), left = NSPoint(x: 4.6, y: 5.2), right = NSPoint(x: 13.4, y: 5.2)
            let edges = NSBezierPath()
            edges.lineWidth = 1.5
            edges.lineCapStyle = .round
            edges.move(to: top); edges.line(to: left)
            edges.move(to: top); edges.line(to: right)
            edges.move(to: left); edges.line(to: right)
            edges.stroke()
            for (center, radius) in [(top, 2.7), (left, 2.3), (right, 2.3)] as [(NSPoint, CGFloat)] {
                NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = Self.menuBarGlyph()
        statusItem.button?.setAccessibilityLabel("TCmodeldesk 工作台")
        statusItem.button?.toolTip = "TCmodeldesk"
        let menu = NSMenu()
        menu.addItem(serviceState)
        menu.addItem(.separator())
        let show = menu.addItem(withTitle: "打开 TCmodeldesk", action: #selector(showWindow), keyEquivalent: "")
        show.target = self
        let again = menu.addItem(withTitle: "重新载入 / 重新连接", action: #selector(reload), keyEquivalent: "")
        again.target = self
        let settings = menu.addItem(withTitle: "数据位置与迁移…", action: #selector(dataSettings), keyEquivalent: "")
        settings.target = self
        menu.addItem(.separator())
        let quit = menu.addItem(withTitle: "退出 TCmodeldesk", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        statusItem.menu = menu
    }

    private func buildMenu() {
        let bar = NSMenu()
        let app = NSMenuItem()
        bar.addItem(app)
        let menu = NSMenu()
        menu.addItem(withTitle: "关于 TCmodeldesk", action: #selector(about), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "数据位置与迁移…", action: #selector(dataSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "连接已有工作台…", action: #selector(chooseWorkspace), keyEquivalent: "o")
        menu.addItem(withTitle: "使用应用独立工作台", action: #selector(useManagedWorkspace), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "隐藏 TCmodeldesk", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        menu.addItem(withTitle: "退出 TCmodeldesk", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
        viewMenu.addItem(withTitle: "关闭窗口（应用继续在程序坞和菜单栏运行）", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
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
                throw NSError(domain: "TCmodeldesk", code: Int(process.terminationStatus),
                    userInfo: [NSLocalizedDescriptionKey: object?["error"] ?? "后台操作未完成，原数据已保留。"])
            }
            do {
                return try JSONDecoder().decode(BackendConnection.self, from: data)
            } catch {
                throw NSError(domain: "TCmodeldesk", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "后台没有返回可用的连接信息，请重试或查看数据位置。"])
            }
        }.value
    }

    private func start(action: String = "connect", destination: URL? = nil) {
        guard !working else { return }
        working = true
        serviceState.title = action == "migrate" ? "正在迁移数据…" : "正在连接工作台…"
        status.stringValue = action == "migrate" ? "正在校验并迁移数据，请勿退出…" : "正在连接工作台…"
        status.isHidden = false
        detailLabel.isHidden = true
        failureButtons.isHidden = true
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
                window.subtitle = ownsConnection ? "独立工作台" : "已连接现有工作台"
                serviceState.title = ownsConnection ? "独立工作台正在运行" : "已连接外部工作台 · 退出仅断开"
            } catch {
                webView.isHidden = true
                status.isHidden = false
                status.stringValue = action == "migrate" ? "数据迁移未完成，原数据已保留" : "没有连上工作台"
                detailLabel.stringValue = error.localizedDescription + "\n连接状态尚未确认；已有后台可能仍在运行。可以重试，或查看数据位置后核对。"
                detailLabel.isHidden = false
                failureButtons.isHidden = false
                serviceState.title = "连接未完成 · 打开窗口处理"
                showWindow()
            }
        }
    }

    @objc private func dataSettings() {
        guard !working else { return }
        let alert = NSAlert()
        alert.messageText = "数据位置"
        let path = connection.map { $0.managed ? URL(fileURLWithPath: $0.workspace).deletingLastPathComponent().path : $0.workspace } ?? "尚未连接"
        alert.informativeText = "当前位置：\(path)\n\n默认位置：~/Library/Application Support/TCmodeldesk/Data\n\n会迁移：应用设置、模型配置、缓存和全局运行数据\n不会搬移：项目源码与项目内 .model-desk 历史、系统钥匙串、第三方 CLI 账户\n\n迁移前先校验并复制；失败会回退，原目录始终保留作为备份。迁移期间请勿退出。"
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
        panel.message = "将在所选位置新建 TCmodeldesk Data；不覆盖已有目录。请先暂停目标并结束任务。"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let parent = panel.url {
            start(action: "migrate", destination: parent.appendingPathComponent("TCmodeldesk Data"))
        }
    }

    @objc private func chooseWorkspace() {
        guard !working else { return }
        let panel = NSOpenPanel()
        panel.title = "选择已有 TCmodeldesk／AAD 安装目录"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            switchWorkspace(to: url)
        }
    }
    @objc private func useManagedWorkspace() {
        guard !working else { return }
        switchWorkspace(to: nil)
    }
    private func stopOwnedBackend() async throws {
        guard let connection, ownsConnection else { return }
        let encoded = try JSONEncoder().encode(connection)
        guard let value = String(data: encoded, encoding: .utf8) else { throw CocoaError(.coderInvalidValue) }
        _ = try await runBackend(["stop", "--preferences", preferences.path, "--connection", value])
    }
    private func switchWorkspace(to destination: URL?) {
        guard !working else { return }
        working = true
        Task {
            do {
                try await stopOwnedBackend()
                connection = nil
                workspace = destination
                if let destination { UserDefaults.standard.set(destination.path, forKey: "ExternalWorkspace") }
                else { UserDefaults.standard.removeObject(forKey: "ExternalWorkspace") }
                working = false
                start()
            } catch {
                working = false
                showWindow()
                showError(error.localizedDescription)
            }
        }
    }
    @objc private func retryConnection() { if !working { start() } }
    private func showConnectionLost(_ detail: String) {
        guard connection != nil, !working else { return }
        webView.isHidden = true
        status.isHidden = false
        status.stringValue = "工作台页面已断开"
        detailLabel.stringValue = detail + "\n页面断开不会主动停止任务；后台是否仍在运行，请重新连接核对。"
        detailLabel.isHidden = false
        failureButtons.isHidden = false
        serviceState.title = "页面已断开 · 可重新载入"
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        showConnectionLost("无法载入工作台页面：" + error.localizedDescription)
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        showConnectionLost("页面加载中断：" + error.localizedDescription)
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        showConnectionLost("页面进程已退出，正在重新载入。")
        webView.reload()
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if connection != nil {
            webView.isHidden = false
            failureButtons.isHidden = true
            detailLabel.isHidden = true
            status.isHidden = true
            serviceState.title = ownsConnection ? "独立工作台正在运行" : "已连接外部工作台 · 退出仅断开"
        }
    }
    @objc private func reload() {
        if connection != nil { status.isHidden = true; failureButtons.isHidden = true; detailLabel.isHidden = true; webView.isHidden = false; webView.reload() } else { start() }
    }
    @objc private func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc private func about() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "TCmodeldesk", .applicationVersion: "0.3.2",
            .credits: NSAttributedString(string: "原生 macOS 窗口 · 本地多智能体工作台\n关闭窗口后保留程序坞和菜单栏入口；退出先检查运行任务。\n基于 Gambler134201/multi-agent-collaboration · MIT")])
    }
    private func showError(_ message: String) { let alert = NSAlert(); alert.messageText = "操作未完成"; alert.informativeText = message; alert.runModal() }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if working { showError("正在准备或迁移数据，请等待操作结束。"); return .terminateCancel }
        guard ownsConnection else { return .terminateNow }
        guard !quitting else { return .terminateCancel }
        quitting = true
        working = true
        serviceState.title = "正在检查任务并退出…"
        Task {
            do {
                try await stopOwnedBackend()
                NSStatusBar.system.removeStatusItem(statusItem)
                NSApp.reply(toApplicationShouldTerminate: true)
            } catch {
                quitting = false
                working = false
                serviceState.title = "仍在运行 · 退出未完成"
                NSApp.reply(toApplicationShouldTerminate: false)
                showWindow()
                showError((powerOffRequested ? "关机或注销已被取消：" : "") + error.localizedDescription)
                powerOffRequested = false
            }
        }
        return .terminateLater
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
struct TCmodeldeskApp {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
        withExtendedLifetime(delegate) {}
    }
}
