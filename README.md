<div align="center">

# fn-fancontrol · 飞牛 NAS 风扇控制

**按 CPU / 显卡 / 硬盘温度自动调节机箱与 CPU 风扇转速**

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-x86-lightgrey.svg)](#兼容性)
[![fnOS](https://img.shields.io/badge/fnOS-%E2%89%A51.1.3100-green.svg)](https://www.fnnas.com/)
[![Version](https://img.shields.io/badge/version-1.9.0-orange.svg)](CHANGELOG.md)

[简体中文](README.md) · [English](README.en.md)

</div>

---

## 为什么做这个

成品 NAS 的风扇策略通常只有「静音 / 均衡 / 全速」三档，而且**只看 CPU 温度**。

但 NAS 里最怕热的恰恰不是 CPU，是硬盘：WD 官方建议硬盘长期工作温度低于 50 °C，
而很多机器在 CPU 闲逛的时候，硬盘已经悄悄爬到 55 °C 了 —— 因为机箱风扇压根没考虑硬盘。

这个应用让你**用硬盘温度驱动机箱风扇**，同时 CPU 风扇照常跟 CPU 走，
两条曲线互不干扰。设计思路参考了 [FanControl](https://github.com/Rem0o/FanControl.Releases)，
但刻意只保留最核心的部分：**温度源 + 控温曲线 + 配置持久化**，不做过度设计。

## 功能特性

| | |
|---|---|
| 🌡️ **温度源覆盖整机** | CPU（coretemp / k10temp / PECI）、显卡（amdgpu / i915 / xe / nouveau / nvidia-smi）、硬盘（drivetemp / nvme / smartctl 兜底），外加主板（SYSTIN / CPUTIN / T_SENSOR）、内存（jc42 / spd5118）与 ACPI 热区，**每路一个独立开关** |
| 🔍 **硬件自检向导** | 首次启动自动列出控制器暴露的**全部** PWM 通道、实际转速与 BIOS 绑定的温度源，可逐个通道全速试转识别停转的风扇 |
| ⏬ **标定含 0 %** | 标定会把风扇降到 0 %，因此能直接查出风扇是否支持停转（0 % 读到 0 RPM），标定结果会保存并在下次打开时回显 |
| 💽 **按盘选择，不按位置** | 硬盘用型号 + 序列号标识（`/dev/disk/by-id`），不是 `sda`/`sdb`：换盘位、换线或改启动顺序后，选择依然对应同一块盘 |
| 🖥️ **每块显卡一个温度源** | 单卡时是一张「显卡」卡片，多卡时自动拆成「显卡1 / 显卡2 …」，各自独立开关且卡内显示型号，不会再出现两个勾选框管同一件事 |
| 🎴 **显卡按型号识别** | GPU 用 PCI 型号名标识（如 `Intel DG1 [Iris Xe Graphics]`），不是笼统的 `i915`；多张显卡可分别勾选 |
| 👁️ **关掉也照样显示** | 未参与调速的温度源仍会读取并显示读数，不会把「机器没这个传感器」和「你没打开」混为一谈 |
| 🧭 **假读数不会混进来** | 没接传感器的输入（NCT67xx 空置的 AUXTIN 会稳定读在 111–115 °C）以及 PECI 这类与 CPU 重复的读数都不会列出来 —— 一个假读数足以把所有风扇拉满 |
| 🎚️ **两点转速标定** | 硬件检测分别以约 30 % 与 100 % 转速测量转速，给出每路的**最低 / 最高转速**和「调速是否有效」，用来确认 PWM 真的生效 |
| 📍 **工作点可视化** | 曲线上实时标出当前温度与转速对应的点，一眼看出风扇此刻被要求跑在哪里 |
| 📈 **可视化曲线** | 每个风扇一条独立曲线，网页上直接拖动节点调整，支持 2–8 个节点 |
| 🎛️ **多种模式** | 温度曲线 / 固定转速 / BIOS 自动，逐风扇独立设置 |
| 🔀 **多源取最高** | 一个风扇可绑定多个温度源，取其中的最高值（例如机箱风扇同时看硬盘和 CPU） |
| 🐢 **温度迟滞** | 降温超过设定幅度才允许降速，避免风扇反复变速的噪音 |
| 💾 **硬盘友好** | 硬盘温度单独设置较长轮询间隔，减少唤醒休眠硬盘；smartctl 路径使用 `-n standby` 绝不唤醒 |
| 🛡️ **失效保护** | 温度源全部失效时风扇升到设定的保护转速，界面同时告警 |
| ↩️ **安全交还** | 停止 / 卸载应用时自动把风扇交还主板 BIOS；从配置里移除某个风扇时，该通道精确恢复成接管前的状态 |
| 🪶 **零依赖** | 后端纯 Python 3 标准库，前端原生 JS 无构建步骤，不需要 nodejs / python312 运行时应用 |

## 界面

> 📷 *截图待补充 —— 如果你愿意贡献截图，欢迎提 PR。*

<!--
把截图放到 docs/screenshots/ 后替换成：
![总览](docs/screenshots/overview.png)
![曲线编辑](docs/screenshots/curve.png)
-->

主界面分三块：

- **温度源** —— CPU / 显卡 / 硬盘三张大卡片，各自独立开关；主板、内存、ACPI 热区收在「其它温度源」折叠区；硬盘逐块温度与勾选列在最下面
- **风扇** —— 每个通道一张卡片：转速、转速、依据温度、模式、温度源、最低/最高转速、可拖动的曲线图
- **全局设置** —— 控制周期、硬盘轮询间隔、温度迟滞、失效保护转速

## 安装

### 前置条件

- 飞牛 fnOS（`os_min_version ≥ 1.1.3100`，实测于 `1.2.0604`）
- x86 主板，风扇挂在本项目支持的控制器上（见 [兼容性](#兼容性)）
- 需要**管理员**账号安装（第三方应用安装本身要求管理员）

### 从 Release 安装

1. 到 [Releases](../../releases) 下载 `fn-fancontrol-<版本>.fpk`（或直接用仓库 `dist/` 里的文件）
2. 飞牛 **应用中心 → 手动安装**，选择该 `.fpk`，选一个存储空间
3. 安装完成后打开 **风扇控制**

也可以走命令行：

```bash
sudo appcenter-cli install-fpk fn-fancontrol-1.2.0.fpk -v 3   # -v 是存储空间序号
sudo appcenter-cli start fn-fancontrol
```

> ⚠️ 实测 `appcenter-cli install-fpk` 对**已安装的同名应用不会执行升级**，
> 更新版本需要先 `uninstall` 再装（或用应用中心界面的升级入口）。
> 卸载不会删除 `config.json`，重装后曲线配置照旧。

## 使用

**首次启动必须先做一次转速标定。** 这不是可以跳过的引导：一路风扇到底听不听话，
恰恰是装完之后最难自己判断的事 —— 界面看着在调速、风扇却纹丝不动，用户只会以为
软件坏了。所以在点过「主动检测」并「应用并保存」之前，向导不会关闭，也进不了主界面。

标定会把每路风扇先拉满、再降到 0 % 各测一次，得出转速范围，并判断这一路是否真的受
PWM 控制、是否支持停转。**只有一种情况不阻塞**：机器上根本没有可调速的 PWM 通道
（主板由 EC 管理、驱动没加载等）—— 那种情况下没有风扇会被调错，向导改为把诊断信息讲清楚。

进入主界面后：

1. **确认温度源** —— CPU / 显卡 / 硬盘是三张大卡片；主板、内存、ACPI 热区收在
   「其它温度源」折叠区里，需要哪一路就展开勾选。硬盘逐块温度列在最下面，
   取消勾选不想参与调速的盘
2. **确认接头归属** —— 首次安装会按主板 BIOS 的配置自动推断（跟 CPU 核心温度的判为 CPU 风扇，
   其余判为机箱风扇），但**建议核对一下**哪张卡片对应哪个物理接头：给 CPU 加压或用 `stress-ng`，
   看哪个风扇转速跟着动
3. **调曲线** —— 拖动圆点，双击空白处加节点，右键节点删除
4. **点击保存** —— 配置立即生效并落盘

> 🔍 任何时候点右上角「**硬件检测**」都能看到完整清单：所有温度传感器（包括没读数的，
> 折叠起来并注明原因 —— 读数为 0、超出合理范围按未接传感器处理、与 CPU 温度重复等）
> 和所有 PWM 通道（无转速的同样折叠，可能是没接风扇，也可能是风扇停转）。

> 💡 **推荐起步配置**：机箱风扇绑 `硬盘`，CPU 风扇绑 `CPU`。
> 硬盘曲线可以设成 `40°C → 30%`、`48°C → 60%`、`55°C → 100%`。

## 兼容性

能不能用，取决于**风扇挂在哪颗控制器上，以及它的内核驱动有没有被适配**。
各驱动家族的 `pwm_enable` 语义**并不相同**（写错值可能把风扇控制整个关掉），
所以每个家族都有独立配置，数值全部取自内核文档 `Documentation/hwmon/*.rst`：

| 控制器家族 | 内核驱动 | 手动 | 自动 | 状态 |
| --- | --- | :---: | :---: | --- |
| Nuvoton NCT6775 / 6776 / 6779 / 679x / 6106 | `nct6775` | `1` | `5` Smart Fan IV | ✅ 已实测 |
| ITE IT87xx（IT8603E … IT87952E 全系） | `it87` | `1` | `2` ※ | ✅ 已适配 |
| Fintek F718xx / F8000 / F81865F | `f71882fg` | `1` | `2` | ✅ 已适配 |
| Fintek F71805F / F71872F | `f71805f` | `1` | `2` | ✅ 已适配 |
| Winbond W83627EHF / DHG / UHG / W83667HG | `w83627ehf` | `1` | `2` | ✅ 已适配 |
| Winbond W83627HF / THF / W83697HF | `w83627hf` | `1` | `2` | ✅ 已适配 |
| SMSC SCH5627 / SCH5636 | `sch5627` / `sch5636` | `1` | `2` | ✅ 已适配 |
| **Nuvoton NCT6683 / 6686 / 6687** | `nct6683` | — | — | ⚠️ **仅监控** |
| 其它带 `pwmN` 的 hwmon 节点 | 任意 | `1` | `2` | ⚠️ 通用兜底，未验证 |
| ACPI `PNP0C0B` 风扇对象 | — | — | — | ❌ DSDT 空壳 |
| 风扇由 EC 管理（笔记本 / 部分迷你主机） | — | — | — | ❌ 没有 pwm 节点 |

> 除 NCT6775 家族外，其余家族是按内核文档适配的，**尚未在真实硬件上验证**。
> 如果你有 ITE / Fintek / Winbond 的机器，非常欢迎反馈结果（见[贡献](#贡献)）。

本项目的开发与实测环境：

```
主板   ASUSTeK TUF B365M-PLUS GAMING (B365, LGA1151)
CPU    Intel CC150  8C/16T
芯片   Nuvoton NCT6796D @ 0x2e:0x290
控制器 6 路 PWM，其中 2 路接有风扇
系统   飞牛 fnOS 1.2.0604 / 内核 6.18.18.c1032-trim
```

### 两个值得知道的坑

**① ITE 的自动模式只对老芯片有效。** `it87` 驱动的 “Smart Guardian” 仅实现了
IT8705F ≤ rev F、IT8712F ≤ rev G，新芯片写 `2` 会被驱动拒绝。应用对此的处理是
逐级回退：先恢复接管前记录的状态 → 再试驱动自动模式 → 都不行就**直接给全速**。
最后这一条很关键：没有它，一块不支持自动模式的 ITE 主板上「停止应用」会把风扇
永久留在最后一次写入的低转速上。

**② ITE 主板常需要额外一步。** `it87` 驱动可能因 ACPI 已占用 SuperIO 的 I/O 端口
而拒绝接管，`dmesg` 里会看到 `ACPI: resource conflict`：

```bash
sudo modprobe it87 ignore_resource_conflict=1   # 只影响该驱动，风险相对小
# 或在内核启动参数加 acpi_enforce_resources=lax（影响面更大）
```

**NCT6683 / 6686 / 6687 为什么只读**：内核 `nct6683` 驱动的文档明确写着，Intel EC
固件的寄存器布局与 Nuvoton 数据手册不符，**从操作系统写入任何值都被视为风险过高
而禁用**（驱动根本不导出可写的 `pwmN`）。这类主板（ASRock B650/X670E、MSI B550/X670
等）应用会识别出控制器并如实告诉你只能监控。

## 从源码构建

需要飞牛官方的打包工具 `fnpack`（随 fnOS 提供，位于 `/usr/local/bin/fnpack`）。

```bash
git clone https://github.com/LiuFudi/fn-fancontrol.git
cd fn-fancontrol
./build-fpk.sh                     # 产出 dist/fn-fancontrol-<版本>.fpk
```

`build-fpk.sh` 会：

1. 从 `package/manifest` 读取 `appname` 与 `version`
2. 校验 `app/server/fancontrold.py` 里的 `VERSION` 常量与 manifest 一致（不一致会告警）
3. 调用 `fnpack build`，把产物重命名为 **`<appname>-<version>.fpk`** 放进 `dist/`
4. 反向校验成品内 manifest 的版本与文件名一致
5. 清理历史遗留的无版本号产物，但保留其它版本的产物

图标可以重新生成（纯 Python，无第三方依赖）：

```bash
python3 tools/make_icons.py
```

## 工作原理

```
浏览器 iframe
   └─ 飞牛统一网关 /app/fn-fancontrol   ← 校验登录态 + 转发 X-Trim-Isadmin
        └─ Unix socket  package/target/app.sock   ← 不监听任何 TCP 端口
             └─ fancontrold.py (以 root 运行)
                  ├─ 控制循环线程：读温度 → 曲线插值 → 写 /sys/class/hwmon/*/pwmN
                  └─ HTTP 线程：静态页面 + /api/*
```

**控制循环**每 `interval` 秒执行一次：读取启用的温度源 → 对每个风扇取其绑定源中的最高温 →
代入曲线做线性插值得到百分比 → 按 `min_duty` / `max_duty` 夹取为 0–255 的转速 →
仅在数值变化时才写 `pwmN`。降速前还要通过温度迟滞检查。

**为什么必须是 root**：写 `/sys/class/hwmon/*/pwm*`（`0644 root:root`）、
`modprobe nct6775`、以及读硬盘 SMART 都需要 root。这段权限属于**应用**而不是用户 ——
装一次之后守护进程常驻 root，日常调节只看网页，不需要 sudo、不需要命令行。

## 配置

配置以 JSON 保存在应用配置目录（`/volN/@appconf/fn-fancontrol/config.json`），
界面保存时原子写入。手工编辑也会在下次保存时被服务端规范化。

```jsonc
{
  "version": 1,
  "enabled": true,          // 总开关；false 时全部交还 BIOS
  "interval": 3,            // 控制周期（秒）
  "hdd_interval": 60,       // 硬盘温度读取间隔（秒），调大可减少唤醒休眠硬盘
  "hysteresis": 3,          // 温度迟滞（°C）
  "fail_safe_duty": 255,    // 所有温度源失效时的转速
  "sources": {
    "cpu": true,
    "gpu": false,
    "hdd": true,
    "hdd_devices": []       // 空数组 = 使用全部硬盘中的最高温度
  },
  "fans": [
    {
      "channel": 2,         // SuperIO 通道号
      "name": "CPU_FAN",
      "mode": "curve",      // curve | manual | auto
      "source": ["cpu"],    // 可多选，取最高温
      "points": [[30, 20], [45, 35], [60, 60], [75, 100]],   // [温度°C, 转速%]
      "min_duty": 60,
      "max_duty": 255,
      "manual_duty": 128
    }
  ]
}
```

## HTTP 接口

服务通过 Unix socket 暴露一个小 JSON API，由飞牛网关鉴权后转发：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ping` | 存活探测 |
| GET | `/api/status` | 温度源、风扇实时状态、当前配置 |
| GET | `/api/config` | 仅配置 |
| GET | `/api/hardware` | 探测到的 SuperIO 通道与硬盘清单 |
| POST | `/api/config` | 校验 → 原子落盘 → 立即生效 |
| POST | `/api/action` | `{"action":"restore-auto"}` / `{"action":"refresh-hardware"}` |

守护进程另有独立子命令，便于排错（均需 root）：

```bash
python3 package/app/server/fancontrold.py status      --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py init-config --appdest <dir> --etc <dir> [--force]
python3 package/app/server/fancontrold.py restore     --appdest <dir> --etc <dir>
python3 package/app/server/fancontrold.py run --host 127.0.0.1 --port 8099   # 本地调试
```

## 安全设计

| 场景 | 行为 |
|---|---|
| 停止 / 卸载应用 | 守护进程退出前把它**实际驱动过**的通道写回 `pwmN_enable=5`（BIOS 自动）；从未管理过的通道保持原样不动 |
| 守护进程被强杀 | `cmd/main` 检测到需要 `kill -9` 时兜底执行全量 `restore` |
| 从配置移除风扇 | 该通道立即恢复成**接管前记录的状态**（enable + duty 一并还原） |
| 温度源全部失效 | 升到 `fail_safe_duty`（默认全速），界面告警 |
| 非管理员访问 | 服务端校验网关注入的 `X-Trim-Isadmin`，为假或缺失一律 `403`（fail-closed） |
| 本机其它账号 | 应用 socket 为 `0600 root:root`，无法绕过网关直连；访问控制交还飞牛登录态 |
| 路径穿越 | 静态文件服务对目标路径做 `realpath` 边界校验 |

## 常见问题

<details>
<summary><b>界面提示「未找到风扇控制器」</b></summary>

先确认驱动是否加载成功：

```bash
sudo modprobe nct6775
dmesg | grep -iE "nct|it87|f718|w836|sch56"   # 芯片有没有被识别
cat /sys/class/hwmon/*/name                    # 驱动注册了哪些节点
ls /sys/class/hwmon/*/name | xargs cat | grep -i nct
```

如果 `dmesg` 里出现 `ACPI: resource conflict`，说明 ACPI 占用了 SuperIO 的
I/O 端口，需要在启动参数里加 `acpi_enforce_resources=lax` 后重启。

如果界面**点名列出了某个芯片**，说明它已被识别但没有可用的写入通道，见 [兼容性](#兼容性)。
</details>

<details>
<summary><b>打开应用显示 Bad Gateway（502）</b></summary>

网关连不上应用，通常是应用没起来或 socket 没建立：

```bash
sudo appcenter-cli status fn-fancontrol
ls -la /var/apps/fn-fancontrol/target/app.sock    # 关键
sudo /var/apps/fn-fancontrol/cmd/main status; echo "exit=$?"
```

若状态显示 running 但 socket 不存在，是 1.7.1 修掉的缺陷：PID 文件跨重启保留，
而内核会复用 PID —— 重启后旧 PID 被别的服务占用时，只靠 `kill -0` 会误判应用仍在运行，
应用中心便跳过启动。恢复：

```bash
sudo appcenter-cli stop fn-fancontrol
sudo rm -f /var/apps/fn-fancontrol/var/app.pid
sudo appcenter-cli start fn-fancontrol
```
</details>

<details>
<summary><b>能读到转速，但改 PWM 没反应</b></summary>

点**硬件检测 → 主动检测**：应用会以约 30% 与 100% 转速各测一次，并在「有转速但
调不动」时直接列出原因。按提示对照即可：

| 检测结果 | 处理 |
| --- | --- |
| 写入转速 X 但读回 Y | 寄存器没接受写入，驱动或芯片忽略了它 |
| 写 `pwmN_enable` 被拒绝 | 芯片进不了手动模式，仍由 BIOS 自动曲线控制 |
| 模式显示 **DC** | DC 电压调速模式，部分主板不响应转速写入。试 `echo 1 > .../pwmN_mode` 切到 PWM 后重测 |
| 寄存器读写正常、转速却不变 | 问题在风扇或接线：3 针风扇插在 PWM 接头上时第 4 根线不起作用，风扇恒速运转 |

手工最小排查（`hwmonX` 换成实际节点）：

```bash
H=/sys/class/hwmon/hwmonX
cat $H/pwm1_mode; cat $H/pwm1_enable
echo 1 > $H/pwm1_enable
echo 255 > $H/pwm1; sleep 5; cat $H/fan1_input
echo 76  > $H/pwm1; sleep 5; cat $H/fan1_input
```

两次转速相同 → 写入没到达风扇；不同 → 调速其实是好的，问题在曲线或配置上。

> 判断转速是否稳定请给足等待时间：大风扇降速要好几秒，读太早会拿到**上一个转速**。
</details>

<details>
<summary><b><code>pwmN_enable</code> 回读值和写入值不一致</b></summary>

已知的驱动行为差异。在 NCT6796D 上写入 `1`（手动）会读回 `0`。
本应用因此**不依赖回读值判断模式**，而是自行记录已下发的模式，
界面上显示的是应用自己的有效模式。
</details>

<details>
<summary><b>有 4 针风扇但转速为 0</b></summary>

该接头没插风扇，或者风扇没有测速线。默认配置只包含有转速读数的通道，
如果某个风扇当前停转导致被漏判，可以在界面底部用「添加该通道」手动加回，
不想要时点卡片上的「移除」即可。
</details>

<details>
<summary><b>担心硬盘被频繁唤醒</b></summary>

把「全局设置 → 硬盘温度读取间隔」调大（例如 300 秒）。
另外若硬盘能走 `drivetemp` / `nvme` 内核驱动，走的是纯 sysfs 读取；
只有没有 hwmon 节点的盘才会退回 `smartctl -n standby`，该参数保证不会唤醒休眠盘。
</details>

## 目录结构

```
fn-fancontrol/
├── LICENSE                   GPL-3.0
├── README.md / README.en.md
├── CHANGELOG.md
├── build-fpk.sh              构建 + 版本化命名
├── package/                  fnpack 源码树（会被打包进 fpk）
│   ├── manifest
│   ├── ICON.PNG / ICON_256.PNG
│   ├── app/
│   │   ├── server/           守护进程（纯标准库）
│   │   │   ├── fancontrold.py    控制循环 + HTTP API + 静态页面
│   │   │   ├── fanhardware.py    SuperIO / hwmon / 温度源
│   │   │   └── fanconfig.py      配置校验 + 曲线插值
│   │   └── ui/               前端（原生 JS）
│   ├── cmd/                  生命周期脚本
│   └── config/               privilege / resource
├── tools/make_icons.py       纯 Python 图标生成
└── dist/                     构建产物
```

## 贡献

欢迎 Issue 和 PR，尤其是：

- **硬件适配** —— 如果你有 ITE / Fintek / 其它 SuperIO 的机器，欢迎提供
  `dmesg`、`ls /sys/class/hwmon/*/name`、`sensors` 输出，一起评估适配
- **截图** —— 界面截图会让 README 完整很多
- **英文文档校对** —— `README.en.md` 欢迎润色

## 免责声明

本软件直接读写主板 SuperIO 的 PWM 寄存器。作者已尽力保证安全设计
（失效保护、停止即交还 BIOS、最低转速限制），但**因使用本软件导致的任何
硬件损坏或数据损失，作者不承担责任**。请自行评估风险，尤其是首次在非验证过的
主板上使用时。

## 打赏支持

应用界面右上角有一个 ❤ 打赏入口。它是**纯本地**的：

- 文案来自 `app/ui/donate.json`，不联网、不上报、不统计点击；
- 打赏完全自愿，**不影响任何功能**，也不会改变软件的任何行为。

入口**始终显示**，程序里没有关闭它的开关；它只有一个小按钮，不会自动弹出，也不会打断任何操作。

## 许可证

[GNU General Public License v3.0 或更新版本](LICENSE) © 2026 [LiuFudi](https://github.com/LiuFudi)

fn-fancontrol 是自由软件：你可以依照自由软件基金会发布的 GNU 通用公共许可证
（第 3 版，或你选择的任何更新版本）的条款重新发布和/或修改它。

fn-fancontrol 分发时希望它有用，但不提供**任何担保**，甚至不带对适销性或特定用途
适用性的默示担保。详见 GNU 通用公共许可证。

你应当已随本程序收到 GNU 通用公共许可证的副本；如果没有，
请见 <https://www.gnu.org/licenses/>。

> **关于许可证变更**：1.9.0 及更早的版本以 **MIT** 许可证发布。许可证变更仅对
> 新版本生效 —— 已经获得那些版本的人依然保留 MIT 授予的权利，这一点无法撤销。
