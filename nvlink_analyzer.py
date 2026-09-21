import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# 設定要檢查的 Slot 範圍 (16 ~ 26)
SLOTS = [str(i) for i in range(16, 27)]
TOTAL_ACP_COUNT = 288

# ------------------------------------------------------------
# Pass 條件的期望值 (依規格：Speed 328G / MTU 256 / Type nvl /
# CodeStatus 0 / Message "No issue was observed")
# ------------------------------------------------------------
EXPECTED_SPEED = "328G"
EXPECTED_MTU = "256"
EXPECTED_TYPE = "nvl"
EXPECTED_DIAG_CODE = "0"
EXPECTED_DIAG_MSG = "No issue was observed"


def _extract_acp_block(text, header_regex):
    """
    從指令輸出的表格標題那一行開始，逐行往下收集 acpN 開頭的資料行，
    只要遇到「非 acpN 開頭」且「非分隔線(---)」且「非表頭本身」的行，
    就視為這個表格已經結束並停止收集。

    這樣可以避免把同一個檔案裡，後面出現的其他指令輸出
    (例如 link-diagnostics 或 ibstat 區塊剛好也有 acpN 開頭的行) 誤抓進來。
    """
    header_matches = list(re.finditer(header_regex, text, re.IGNORECASE))
    if not header_matches:
        return None  # 找不到這個表格

    remaining = text[header_matches[-1].start():]
    lines = remaining.splitlines()
    acp_lines_map = {}
    started = False  # 是否已經開始出現 acpN 資料行

    for line in lines:
        s = line.strip()

        if not s:
            if started:
                break
            else:
                continue

        m = re.match(r'^(acp\d+)\b', s)
        if m:
            acp_lines_map[m.group(1)] = s
            started = True
            continue

        # 分隔線 (例如 "--------- ----- -----") 或表格標題本身，跳過繼續往下找
        if re.match(r'^-+(\s+-+)*$', s) or re.search(header_regex, s, re.IGNORECASE):
            continue

        # 其他情況：
        # - 若已經開始收集 acp 資料，代表遇到了下一個指令/提示字元，表格結束
        # - 若都還沒開始收集，可能是標題上方殘留文字，繼續往下找
        if started:
            break

    return acp_lines_map


def _check_interface_line(line):
    """
    解析單一行 nv show interface 的資料，回傳不符合 Pass 條件的欄位清單。
    欄位順序: acpN  State  Speed  MTU  Type  [Description 通常為空]  LogicalState  PhysicalState  [Summary]
    """
    tokens = line.split()
    if len(tokens) < 7:
        return [f"欄位格式異常:{line}"]

    _, state, speed, mtu, type_, logical_state, physical_state = tokens[:7]

    problems = []
    if state.lower() != 'up':
        problems.append(f"State:{state}")
    if logical_state.lower() != 'active':
        problems.append(f"LogicalState:{logical_state}")
    if physical_state.lower() != 'linkup':
        problems.append(f"PhysicalState:{physical_state}")
    if speed != EXPECTED_SPEED:
        problems.append(f"Speed:{speed}")
    if mtu != EXPECTED_MTU:
        problems.append(f"MTU:{mtu}")
    if type_.lower() != EXPECTED_TYPE:
        problems.append(f"Type:{type_}")

    return problems


def _check_diag_line(line):
    """
    解析單一行 nv show interface link-diagnostics 的資料，回傳不符合 Pass 條件的欄位清單。
    欄位順序: acpN  CodeStatus  Message(可能含空白，例如 "No issue was observed")
    """
    m = re.match(r'^(acp\d+)\s+(\S+)\s*(.*)$', line)
    if not m:
        return [f"欄位格式異常:{line}"]

    _, code_status, message = m.groups()
    message = message.strip()

    problems = []
    if code_status != EXPECTED_DIAG_CODE:
        problems.append(f"CodeStatus:{code_status}")
    if message != EXPECTED_DIAG_MSG:
        problems.append(f"Message:{message}")

    return problems


def parse_latest_log(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # 1. 擷取最新一次登入的時間 (Last login: ...)
    login_times = re.findall(r'Last login:\s*([^\r\n]+)', content)
    latest_time = login_times[-1].strip() if login_times else "未知/未找到時間標籤"

    results = {
        'timestamp': latest_time,
        'nv_interface': {'pass': True, 'failed_acps': []},
        'link_diagnostics': {'pass': True, 'failed_acps': []},
        'ibstat': {'pass': True, 'failed_switches': []}
    }

    # ==========================================
    # 條件 1: nv show interface
    # 檢查 State: up / Logical State: Active / Physical State: LinkUp
    #      + Speed: 328G / MTU: 256 / Type: nvl
    # ==========================================
    if_lines_map = _extract_acp_block(content, r'Interface\s+State\s+Speed\s+MTU')

    if if_lines_map is not None:
        for acp_num in range(1, TOTAL_ACP_COUNT + 1):
            acp_name = f"acp{acp_num}"
            if acp_name in if_lines_map:
                problems = _check_interface_line(if_lines_map[acp_name])
                if problems:
                    results['nv_interface']['pass'] = False
                    results['nv_interface']['failed_acps'].append(f"{acp_name}({','.join(problems)})")
            else:
                results['nv_interface']['pass'] = False
                results['nv_interface']['failed_acps'].append(f"{acp_name}(未找到紀錄)")
    else:
        results['nv_interface']['pass'] = False
        results['nv_interface']['failed_acps'].append("完全未找到 nv show interface 指令輸出表格")

    # ==========================================
    # 條件 2: nv show interface link-diagnostics
    # 檢查 CodeStatus: 0 / Message: No issue was observed
    # ==========================================
    diag_lines_map = _extract_acp_block(content, r'Interface\s+CodeStatus')

    if diag_lines_map is not None:
        for acp_num in range(1, TOTAL_ACP_COUNT + 1):
            acp_name = f"acp{acp_num}"
            if acp_name in diag_lines_map:
                problems = _check_diag_line(diag_lines_map[acp_name])
                if problems:
                    results['link_diagnostics']['pass'] = False
                    results['link_diagnostics']['failed_acps'].append(f"{acp_name}({','.join(problems)})")
            else:
                results['link_diagnostics']['pass'] = False
                results['link_diagnostics']['failed_acps'].append(f"{acp_name}(未找到紀錄)")
    else:
        results['link_diagnostics']['pass'] = False
        results['link_diagnostics']['failed_acps'].append("完全未找到 link-diagnostics 指令輸出表格")

    # ==========================================
    # 條件 3: sudo ibstat
    # ==========================================
    ibstat_parts = content.split("ibstat")
    if len(ibstat_parts) > 1:
        latest_ibstat_text = ibstat_parts[-1]
        target_switches = ['sx_ib_1', 'sx_ib_2', 'sx_ib_3']

        for sw in target_switches:
            pattern = r"Switch\s+'" + sw + r"'.*?(?=Switch\s+'|\Z)"
            sw_match = re.search(pattern, latest_ibstat_text, re.DOTALL)
            if sw_match:
                sw_block = sw_match.group(0)
                if not re.search(r'Physical state:\s*LinkUp', sw_block, re.IGNORECASE):
                    results['ibstat']['pass'] = False
                    results['ibstat']['failed_switches'].append(sw)
            else:
                results['ibstat']['pass'] = False
                results['ibstat']['failed_switches'].append(f"{sw}(未找到)")
    else:
        results['ibstat']['pass'] = False
        results['ibstat']['failed_switches'].append("完全未找到 ibstat 指令")

    return results


class NVLinkApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NVIDIA Switch NVLink Log 最新數據分析器")
        self.root.geometry("850x650")

        frame_top = tk.Frame(root)
        frame_top.pack(fill="x", padx=10, pady=10)

        lbl_path = tk.Label(frame_top, text="檔案/資料夾路徑:")
        lbl_path.pack(side="left", padx=5)

        self.entry_path = tk.Entry(frame_top, width=50)
        self.entry_path.pack(side="left", padx=5, fill="x", expand=True)

        btn_browse = tk.Button(frame_top, text="瀏覽資料夾...", command=self.browse_folder)
        btn_browse.pack(side="left", padx=5)

        btn_run = tk.Button(frame_top, text="開始分析", bg="#4CAF50", fg="white", font=('Arial', 10, 'bold'), command=self.run_analysis)
        btn_run.pack(side="left", padx=5)

        self.txt_result = tk.Text(root, wrap="word", font=("Consolas", 10))
        self.txt_result.pack(fill="both", expand=True, padx=10, pady=10)

    def browse_folder(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.entry_path.delete(0, tk.END)
            self.entry_path.insert(0, selected_dir)

    def run_analysis(self):
        input_path = self.entry_path.get().strip('"\' ')
        if not input_path:
            messagebox.showwarning("警告", "請先輸入或選擇資料夾路徑！")
            return

        log_dir = Path(input_path)
        if not log_dir.exists():
            messagebox.showerror("錯誤", "找不到該路徑，請確認路徑是否正確。")
            return

        self.txt_result.delete("1.0", tk.END)
        self.txt_result.insert(tk.END, "==============================================================\n")
        self.txt_result.insert(tk.END, "      NVIDIA Switch NVLink Log 自動分析結果 (最新筆數據)      \n")
        self.txt_result.insert(tk.END, "==============================================================\n\n")

        found_any = False
        all_files = list(log_dir.iterdir())

        for slot in SLOTS:
            matching_files = [
                f for f in all_files
                if f.is_file() and re.search(rf'(?<!\d){slot}(?!\d)', f.name)
            ]

            if not matching_files:
                self.txt_result.insert(tk.END, f"[Slot {slot}] ⚠️ 未找到對應 Log 檔案\n")
                self.txt_result.insert(tk.END, "-" * 60 + "\n")
                continue

            found_any = True
            for file_path in matching_files:
                res = parse_latest_log(file_path)
                c1 = res['nv_interface']['pass']
                c2 = res['link_diagnostics']['pass']
                c3 = res['ibstat']['pass']
                is_pass = c1 and c2 and c3

                self.txt_result.insert(tk.END, f"📄 檔案: {file_path.name} (Slot {slot})\n")
                self.txt_result.insert(tk.END, f"🕒 最新紀錄時間: {res['timestamp']}\n")
                self.txt_result.insert(tk.END, f"👉 最終判定: {'✅ PASS' if is_pass else '❌ FAIL'}\n")
                self.txt_result.insert(tk.END, f"  ├─ 1. nv show interface          : {'PASS' if c1 else 'FAIL'}\n")
                self.txt_result.insert(tk.END, f"  ├─ 2. link-diagnostics           : {'PASS' if c2 else 'FAIL'}\n")
                self.txt_result.insert(tk.END, f"  └─ 3. sudo ibstat                : {'PASS' if c3 else 'FAIL'}\n")

                if not is_pass:
                    self.txt_result.insert(tk.END, "\n  🔍 [最新筆 Fail 資訊]:\n")
                    if not c1:
                        failed_acps = res['nv_interface']['failed_acps']
                        s = ", ".join(failed_acps[:10])
                        more = f" ...等共 {len(failed_acps)} 個" if len(failed_acps) > 10 else ""
                        self.txt_result.insert(tk.END, f"     ❌ nv show interface 異常 ACP: {s}{more}\n")
                    if not c2:
                        failed_acps = res['link_diagnostics']['failed_acps']
                        s = ", ".join(failed_acps[:10])
                        more = f" ...等共 {len(failed_acps)} 個" if len(failed_acps) > 10 else ""
                        self.txt_result.insert(tk.END, f"     ❌ link-diagnostics 異常 ACP: {s}{more}\n")
                    if not c3:
                        s = ", ".join(res['ibstat']['failed_switches'])
                        self.txt_result.insert(tk.END, f"     ❌ sudo ibstat 異常 Switch: {s}\n")

                self.txt_result.insert(tk.END, "-" * 60 + "\n")

        if not found_any:
            self.txt_result.insert(tk.END, "路徑下未搜尋到檔名符合 Slot16 ~ Slot26 的檔案。\n")


if __name__ == "__main__":
    root = tk.Tk()
    app = NVLinkApp(root)
    root.mainloop()