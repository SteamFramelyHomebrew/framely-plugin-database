# 贡献

新增和更新向 testing 提交 submodule 固定提交。插件仓库根目录维护 manifest.json，作者自行发布无签名包到具体版本 Release。GitHub 仓库可以省略源码清单的 `downloadUrl`，数据库按 `https://github.com/<owner>/<repo>/releases/download/v<version>/<id>-<version>.framely` 自动生成地址；打包脚本也需把相同的完整地址写入包内 manifest。其他下载地址须显式填写 `downloadUrl`，自定义地址同时填写 `downloadSha256`（完整 `.framely` 文件的 64 位 SHA256 十六进制值）。该哈希只属于源码登记配置，打包时应移除，避免包内包含自身哈希。无需 entries 文件或公钥；默认 GitHub Release 方式无需手写 SHA256。

自动生成地址会读取对应 GitHub Release 附件的 `digest`；显式配置地址使用 `downloadSha256`，未配置哈希的固定 GitHub Release 地址也可读取 Release digest。自动生成地址即使配置了哈希，也必须与 Release 一致。缺少哈希时校验失败，不会退回到仅计算下载结果。

合并前必须实际下载完整插件包并比对整包 SHA256；不可下载、Release 未发布、附件缺失、哈希缺失或不匹配都会阻止自动合并。catalog 沿用已校验的预期哈希。CI 自动检查源码清单与包内清单、运行身份、权限、包边界和载荷哈希；发布时锁定同版本 SHA256，并展示版本与权限变化。不强制维护者逐插件人工源码审核，也不宣称完成源码/二进制可复现验证。

作者应提供实际 Frame 测试记录，并保留许可证。测试覆盖页面、激光/悬停、必要的窗口/输入/通知、启停、更新与数据保存。身份或权限变化应在更新说明中解释。测试通过后将固定提交提升到 main。

发布地址和图片应固定版本，勿使用 latest。相同版本不得覆盖附件；更新提升版本号。删除仓库或附件会影响该版本下载，数据库不会镜像。

图标只配置顶层 `icon`，例如 `"icon": "icon.png"`。将对应 PNG 文件提交到插件仓库，并以同一路径包含在插件包内。数据库根据来源仓库、固定提交和此路径自动生成商店图标的 GitHub Raw 地址，下载并确认它与已校验包内图标完全一致；缺失或不一致会阻止发布和自动合并。无需再配置 `publish.icon`。仅提供旧式 `publish.icon` 的插件仍兼容。

仅修改 `.gitmodules` 与 `plugins/<name>` Git submodule 的 PR，通过主仓库校验后自动合并。普通文件、符号链接、脚本、工作流及其他仓库内容的修改留给人工处理。校验使用主仓库工具检查候选合并内容、清单、发行包、载荷哈希及已发布版本一致性；合并前再次核对 PR 和目标分支提交号，并遵守分支保护。仓库需允许 squash merge。

`publish` 是自动生成分支，向它提交的 PR 会自动关闭。插件变更请提交到 `testing` 或 `main`；合并后自动更新两个渠道的清单。

自动合并只允许来源与目标分支同名：`testing → testing`、`main → main`。跨渠道或其他来源分支的 PR 留给人工处理；校验时和实际合并前均检查此限制。

## 插件归属与自动合并

插件 ID 必须使用 `namespace.name` 格式（小写字母和数字，名称可带点或连字符）；GitHub 仓库名称不限，submodule 路径必须为 `plugins/<插件ID>`。来源仓库须为公开 GitHub 仓库。

首次登记同时验证 PR 作者的仓库维护权限。命名空间前缀绑定仓库所属 GitHub 用户或组织的数字 ID，不能使用 manifest 的 `author` 字符串作为身份。已有前缀的新插件必须属于同一所有者，拥有其他所有者仓库的协作者权限不代表能占用该前缀。每个 GitHub 用户或组织最多占用五个不同前缀，按所有者数字 ID 跨 main/testing 和历史记录合计；同一前缀下增加插件或更新版本不额外占用名额，改 ID、删除插件不会释放旧前缀。第六个新前缀会阻止自动合并和发布。

后续更新、改 ID、删除登记均要求 PR 作者是源仓库所有者或具有 write/maintain/admin 权限的维护者。PR 修改多个插件时必须对每个受影响的仓库都具有维护权限。改 ID 必须使用从未被登记过的新 ID；同时调整 submodule 路径。原 ID 和前缀永久保留归属，删除登记不会释放它们。已登记 ID 不允许换源仓库，仓库转让、重新创建同名仓库或更换来源均不能自动通过。

自动生成的 `publish/ownership.json` 同时记录 main/testing 的归属，并保留历史 ID。启用本规则时，已有登记须先运行 Publish plugin catalogs 工作流生成归属记录；记录未成功发布时，不允许继续修改已有登记。直接修改 publish 的 PR 仍会关闭。

个人仓库所有者可以通过公开仓库信息验证。其他协作者的权限必须通过 GitHub 的 collaborator permission API 验证；数据库自带 GITHUB_TOKEN 不一定被授权查询其他仓库的协作者权限。遇到 401/403/404、权限不明或 API 错误时不会自动合并，不能以 contributors 列表或 Git 提交作者替代权限验证。可以由个人仓库所有者提交 PR；组织仓库或其他协作者需要数据库有权读取该源仓库的权限信息，可在数据库仓库配置可读取这些源仓库 Metadata 的 `PLUGIN_OWNERSHIP_TOKEN` Secret。该凭据仅用于读取身份和协作者权限，数据库合并仍使用自身 GITHUB_TOKEN。
