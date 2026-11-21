# RTX 3070 macOS 26 社区驱动

本仓库发布基于 NullMoth v3.0 的 RTX 3070 第三方实验版。适用环境为 x86_64 macOS 26、RTX 3070（PCI ID `10de:2488`），且已安装 NullMoth 3.0 和对应固件。实测机器使用 macOS 26.6.2、8 GiB 显存和 5K / 160 Hz 显示模式。

[下载 3.0.2 实验版、完整源码和校验文件](https://github.com/assropic-dev/rtx3070-macos-driver/releases/tag/v3.0.2-community) · [完整改进与验证范围](community/RELEASE-3.0.2.md)

## 5K 桌面实测

同一台机器在升级本地显存预备池前后，预热的大缓冲区命中从 4/12 升至 12/12，分配中位数从 159.1 ms 降至 8.7 ms。三轮可见 5K 窗口测试中，超过 100 ms 的翻页间隔从七次降至一次。新版仍出现了一次至少 141 ms 的停顿。原版与本地 Metal 插件各自通过了 480 次跨队列像素校验。[测量结果与上游对照](community/上游对照及验证.md)

## 源码与分发

驱动源码在仓库根目录。安装包使用本机实测的组件，源码另含实验性跟踪与 Boost 变更。外层 PKG 未使用 Developer ID Installer 签名。完整源码测试结果为 256 项通过、8 项失败、10 项未运行。此整合封装只做了载荷与选项校验，完整覆盖安装及回退尚待实机验证。[宿主测试记录](community/evidence/host-tests/host-verification-summary.txt)

- [驱动二进制与上游的逐项哈希](community/comparison/shipped-binary-diff.json)
- [配套性能查看器源码](community/companion/performance-viewer)
- [安装包装配与来源](community/packaging)
- [上游项目](https://github.com/nullmoth/nvidia-macos-driver) · [原 README](https://github.com/nullmoth/nvidia-macos-driver/blob/5289b35091e199644a9a2e414ee6d463c6c4c401/README.md)

本项目公开源码，沿用上游 PolyForm Noncommercial 1.0.0 及各组件的第三方许可。本地开发、测试与发布整理使用了 AI 编程助手。欢迎在本仓库反馈社区版的实测结果。[许可证](LICENSE) · [第三方声明](NOTICE) · [社区版反馈](https://github.com/assropic-dev/rtx3070-macos-driver/issues)
