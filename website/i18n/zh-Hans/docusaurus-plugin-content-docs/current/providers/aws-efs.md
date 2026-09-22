# AWS EFS 工作区存储

`aws_efs` 存储适配器通过官方 AWS EFS CSI driver 接入部署方管理的 EFS 文件系统，
不创建或删除云文件系统、挂载目标或 access point。平台仍通过统一的 mergerfs 路由在
交付沙箱前绑定工作区；引擎不选择存储介质，也不实现 EFS 专属恢复流程。

该适配器目前要求 Kubernetes 和静态配置、可读写的 EFS CSI claim，其 `volumeHandle`
必须与文件系统 ID 完全相同。该适配器未实现 Docker 运行时、包含 access point 或子目录的
volume handle，以及带 `efs:` 前缀的 handle，遇到这些配置会明确拒绝。本地存储和其他
底层文件系统使用 `mounted_volume`。

## 配置底层文件系统

创建 EFS 文件系统及沙箱节点可访问的挂载目标。挂载目标安全组的 TCP 2049 端口只允许
预期客户端访问。EFS 文件系统由部署方管理，生命周期独立于沙箱和 Session。

启用 EFS 工作区前安装官方 CSI driver。以下步骤固定使用 driver `v3.5.0`、Helm chart
`4.5.0`、上游提交 `978c0b0be261904014ffd68af35a7421bbf439a4`。静态配置使用节点
driver，不需要动态创建 access point：

```sh
helm upgrade --install aws-efs-csi-driver aws-efs-csi-driver \
  --repo https://kubernetes-sigs.github.io/aws-efs-csi-driver/ \
  --version 4.5.0 --namespace kube-system --set controller.create=false
```

为实际文件系统 ID 创建静态 PV，并在沙箱命名空间（`ASTRABOX_SANDBOX_SERVER_KUBE_NAMESPACE`）
中创建 PVC。PV 的关键字段如下：

```yaml
spec:
  capacity:
    storage: 20Gi
  volumeMode: Filesystem
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  csi:
    driver: efs.csi.aws.com
    volumeHandle: fs-0123456789abcdef0
    volumeAttributes:
      encryptInTransit: "true"
```

driver 默认启用传输加密，`encryptInTransit: "true"` 只是显式写明这一点。把
`ReadWriteMany` PVC 绑定到这个 PV。Kubernetes 要求声明容量，但它不是 EFS 配额。
等待 claim 进入 `Bound` 状态，然后配置 AstraBox：

```dotenv
ASTRABOX_STORAGE_PROVIDER=aws_efs
ASTRABOX_EFS_FILE_SYSTEM_ID=fs-0123456789abcdef0
ASTRABOX_SANDBOX_WORKSPACE_VOLUME=your-bound-efs-claim
ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY=shared
ASTRABOX_WORKSPACE_MOUNTER_IMAGE=your-immutable-workspace-mounter-image
```

启动时，只有沙箱运行时为 Kubernetes、拓扑为 `shared`、已设置工作区数据卷且文件系统
ID 为 `fs-` 格式时，AstraBox 才接受 `aws_efs`，否则拒绝启动。每次提供挂载方案前，
适配器读取实际 claim 及其 PV：

- 两者都处于 `Bound` 状态、未在删除中、支持 `ReadWriteMany`，并使用 `Filesystem` 模式。
- PV 通过名称、命名空间和 UID 指向这个 claim。
- PV 使用 `efs.csi.aws.com` driver，`volumeHandle` 与配置的文件系统 ID 相同。
- PV 允许写入，并保留传输加密：`encryptInTransit` 不为 `"true"`，或挂载选项包含
  `notls`、`tls=` 时会被拒绝。

同名本地数据卷不能通过这个检查。随后平台使用统一的工作区路由和 OpenSandbox 挂载接口。

## 文件系统行为与验证

EFS 不支持用户扩展属性。路由保留底层文件系统的结果，不模拟这项能力。mergerfs 控制
入口与文件 xattr 分开，不会暴露在沙箱工作区中。工作区需要的文件操作包括读写、原子
rename、权限修改、符号链接和 advisory lock。

AstraBox 将 Agent 程序的原生会话数据保存到平台数据库，因此恢复对话不依赖 EFS。释放
工作区分配时，只删除 AstraBox 为它创建的辅助 Pod 和视图 PV/PVC。底层 claim、其 PV
和 EFS 数据保持不变。

driver 安装成功或 claim 进入 `Bound` 状态，只说明 Kubernetes 能挂载该文件系统。应在
不同节点的沙箱上另行验证工作区持久化，并记录验证结果。

参考：[AWS CSI 静态配置](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/978c0b0be261904014ffd68af35a7421bbf439a4/examples/kubernetes/efs/static_provisioning/README.md)、
[driver 安装](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/978c0b0be261904014ffd68af35a7421bbf439a4/docs/install.md)、
[EFS 网络访问](https://docs.aws.amazon.com/efs/latest/ug/network-access.html)、
[不支持的文件系统功能](https://docs.aws.amazon.com/efs/latest/ug/limits.html)。
