# 加快 overlayfs 快照

在使用 overlayfs 的 Linux containerd 节点上，AstraBox 的快照 differ 从容器可写层生成
发生变化的 OCI 层。这样在暂停沙箱时，无需遍历所有父镜像层中未改变的文件。镜像仓库
上传、快照状态协调和沙箱启动仍然会影响完整的暂停与恢复耗时。

differ 使用 containerd 支持的
[外部 diff 插件接口](https://github.com/containerd/containerd/blob/v2.3.4/docs/PLUGINS.md#proxy-plugins)
和 BuildKit 的 overlay differ。它不替换 containerd、OpenSandbox 控制器或镜像提交组件。
应在每个可能暂停沙箱的节点上安装。

## 要求

- Linux、overlayfs snapshotter，以及完整 copy-up（`metacopy=N`）。
- 以 root 运行的系统服务，可以访问沙箱容器所用的同一个 containerd socket 和文件系统路径。
- 保留现有的 containerd `walking` differ，供不支持的情况使用。
- 构建二进制需要 Go 1.26.3 或更新版本；运行时无需安装 Go。

快速路径处理单个可写 overlay 层，并输出 gzip OCI。其他 snapshotter、无法识别的
挂载选项、只复制元数据、目录重定向和显式可复现时间戳仍使用现有 differ。镜像解包也
继续使用现有 differ。只要 containerd 的首选 differ 列表包含该插件，插件就必须保持
可用；服务停止属于服务故障，不是不支持某种请求。

层格式保留 containerd 原生归档语义，包括文件内容、所有者、权限、链接、删除操作和
`security.capability`。底层归档写入器不保留任意 `user.*` 扩展属性，原生 walking
differ 也有相同限制。

## 构建与安装

从仓库根目录执行：

```bash
cd tools/snapshot-differ
go build -trimpath -o out/astrabox-snapshot-differ ./cmd/astrabox-snapshot-differ
sudo install -D -m 0755 out/astrabox-snapshot-differ /usr/local/libexec/astrabox-snapshot-differ
sudo install -m 0644 astrabox-snapshot-differ.service /etc/systemd/system/astrabox-snapshot-differ.service
sudo systemctl daemon-reload
sudo systemctl enable --now astrabox-snapshot-differ
sudo systemctl is-active astrabox-snapshot-differ
sudo /usr/local/libexec/astrabox-snapshot-differ --check
```

提供的 systemd unit 会创建私有 socket 目录、使用私有挂载命名空间，并连接
`/run/containerd/containerd.sock`。如果沙箱节点使用其他 socket，请在启动前通过
systemd override 调整 `--containerd-address`。进程需要挂载权限，因此 unit 以 root
运行，不能作为无特权应用服务运行。

备份节点的 containerd 配置，加入下面的 proxy plugin，并把 `astrabox-overlay` 放到
现有 diff 服务顺序的最前面。保留其他已配置的 differ，并确保 `walking` 可用。
对于默认顺序：

```toml
[proxy_plugins.astrabox-overlay]
  type = "diff"
  address = "/run/astrabox-snapshot-differ/diff.sock"

[plugins."io.containerd.service.v1.diff-service"]
  default = ["astrabox-overlay", "walking"]
```

如果已有 diff-service 表，请修改原表，不要添加重复的 TOML 表。使用
`containerd --config <path> config dump` 验证配置，再在节点维护窗口重启 containerd。
不要修改其他守护进程的配置，例如 Docker 私有的 containerd。

```bash
sudo systemctl restart containerd
sudo ctr --address /run/containerd/containerd.sock plugins list
```

`io.containerd.differ.v1` 的 `astrabox-overlay` 和 `walking` 两项都必须显示 `ok`。
检查节点就绪状态，并验证完整的沙箱流程：

```bash
astrabox verify-opensandbox-snapshots
```

该命令验证文件系统快照，不改变 [Assistant 休眠](../assistants.md) 的语义；Assistant
会单独持久化原生会话状态并释放计算资源。

## 回滚

恢复原来的 diff 服务顺序，从 containerd 配置中移除 proxy plugin，验证配置后重启
containerd，并确认原生 differ 显示 `ok`。完成这些步骤后，才停止或卸载快照 differ。
如果 containerd 仍把请求路由到该插件，停止服务会中断快照和解包操作。
