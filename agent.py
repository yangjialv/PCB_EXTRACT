#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cadence Allegro 阻抗约束提取工具 - 最终修正版
1. 单端信号只使用 object Name，不使用 member Name
2. 支持 Net、Xnet、DiffPair 三个节点，优先级：Net > Xnet > DiffPair
3. 差分对中的网络如果在 Net/Xnet 中有定义，优先使用那个阻抗值
"""

import xml.etree.ElementTree as ET
import json
import re
import os


class ImpedanceExtractor:
    def __init__(self, dcfx_file, enable_trimming=False,
                 trim_diff_pair_count=0, trim_single_ended_count=0):
        self.dcfx_file = dcfx_file
        self.single_ended = []
        self.differential = []
        self.impedance_priority_map = {}
        self.xnet_object_names = set()

        # === 新增：记录所有差分对中包含的网络 ===
        self.all_diff_pair_nets = set()

        # === 裁剪参数 ===
        self.enable_trimming = enable_trimming
        self.trim_diff_pair_count = trim_diff_pair_count
        self.trim_single_ended_count = trim_single_ended_count
        self.trimmed_single_ended = []
        self.trimmed_differential = []
    def parse_file(self):
        try:
            tree = ET.parse(self.dcfx_file)
            root = tree.getroot()
            print(f"✅ 文件解析成功：{self.dcfx_file}")
            return root
        except Exception as e:
            print(f"❌ 解析失败：{e}")
            return None

    def extract_impedance_value(self, ohm_name):
        match = re.search(r'(\d+)', ohm_name)
        return int(match.group(1)) if match else 0

    def get_tag_name(self, elem):
        return elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

    def find_xml_objects_sections(self, root, section_name):
        results = []
        for elem in root.iter():
            tag_name = self.get_tag_name(elem)
            if tag_name == 'xml-objects':
                name_attr = elem.get('Name', '')
                if name_attr == section_name:
                    results.append(elem)
        return results

    def get_direct_children(self, parent, child_tag):
        children = []
        for child in parent:
            if self.get_tag_name(child) == child_tag:
                children.append(child)
        return children

    def extract_object_info(self, obj_elem):
        """从一个 object 元素提取信息"""
        obj_name = obj_elem.get('Name', '')

        if not obj_name:
            return None

        # 提取阻抗引用
        impedance_ref = ''
        for ref in self.get_direct_children(obj_elem, 'reference'):
            if ref.get('Kind') == 'PhysicalCSet':
                impedance_ref = ref.get('Name', '')
                break

        if not impedance_ref:
            return None

        impedance_value = self.extract_impedance_value(impedance_ref)

        # 提取 member 网络（仅作为参考，不作为独立网络）
        members = []
        for member in self.get_direct_children(obj_elem, 'member'):
            if member.get('Kind') == 'Net':
                members.append(member.get('Name', ''))

        return {
            'object_name': obj_name,
            'impedance_ref': impedance_ref,
            'impedance_value': impedance_value,
            'members': members
        }

    def update_impedance_priority_map(self, net_name, impedance_value, impedance_rule, source, priority):
        """
        更新阻抗优先级映射
        只有当新优先级更高时才更新
        """
        if not net_name:
            return False

        existing = self.impedance_priority_map.get(net_name)

        if existing is None:
            self.impedance_priority_map[net_name] = {
                'impedance': impedance_value,
                'impedance_rule': impedance_rule,
                'source': source,
                'priority': priority
            }
            return True
        elif priority < existing['priority']:
            print(
                f"   🔄 更新 {net_name}: {existing['impedance']}Ω ({existing['source']}) -> {impedance_value}Ω ({source})")
            self.impedance_priority_map[net_name] = {
                'impedance': impedance_value,
                'impedance_rule': impedance_rule,
                'source': source,
                'priority': priority
            }
            return True
        else:
            return False

    def extract_net_impedance(self, root):
        """
        从 Net 节点提取单端阻抗（优先级 1 - 最高）
        只使用 object Name 作为网络名
        """
        print("🔍 正在从 Net 节点提取单端阻抗 (优先级 1)...")

        net_sections = self.find_xml_objects_sections(root, 'Net')
        print(f"   找到 {len(net_sections)} 个 Net 节点")

        count = 0
        for section in net_sections:
            root_nodes = self.get_direct_children(section, 'root')

            for root_node in root_nodes:
                objects = self.get_direct_children(root_node, 'object')
                print(f"   找到 {len(objects)} 个 object")

                for obj in objects:
                    info = self.extract_object_info(obj)
                    if info is None:
                        continue

                    # ✅ 只使用 object Name 作为网络名
                    net_name = info['object_name']

                    if net_name:
                        updated = self.update_impedance_priority_map(
                            net_name=net_name,
                            impedance_value=info['impedance_value'],
                            impedance_rule=info['impedance_ref'],
                            source='Net',
                            priority=1
                        )
                        if updated:
                            count += 1
                            print(f"   ✓ Net: {net_name} ({info['impedance_value']}Ω)")

        print(f"   ✓ 从 Net 节点提取 {count} 条阻抗记录")
        return count

    def extract_xnet_impedance(self, root):
        """
        从 Xnet 节点提取单端阻抗（优先级 2）
        ✅ 只使用 object Name 作为网络名，member 仅作为参考
        """
        print("🔍 正在从 Xnet 节点提取单端阻抗 (优先级 2)...")

        xnet_sections = self.find_xml_objects_sections(root, 'Xnet')
        print(f"   找到 {len(xnet_sections)} 个 Xnet 节点")

        count = 0
        for section in xnet_sections:
            root_nodes = self.get_direct_children(section, 'root')

            for root_node in root_nodes:
                objects = self.get_direct_children(root_node, 'object')
                print(f"   找到 {len(objects)} 个 object")

                for obj in objects:
                    info = self.extract_object_info(obj)
                    if info is None:
                        continue

                    # ✅ 只使用 object Name 作为网络名
                    net_name = info['object_name']

                    # 记录这个 object Name，用于后续差分对网络的优先级判断
                    self.xnet_object_names.add(net_name)

                    if net_name:
                        updated = self.update_impedance_priority_map(
                            net_name=net_name,
                            impedance_value=info['impedance_value'],
                            impedance_rule=info['impedance_ref'],
                            source='Xnet',
                            priority=2
                        )
                        if updated:
                            count += 1
                            print(f"   ✓ Xnet: {net_name} ({info['impedance_value']}Ω) members={info['members']}")

                    # ❌ 删除：不再把 member 作为独立网络记录

        print(f"   ✓ 从 Xnet 节点提取 {count} 条阻抗记录")
        return count

    def extract_diff_pairs(self, root):
        """
        从 DiffPair 节点提取差分对（优先级 3 - 最低）
        同时记录所有属于差分对的网络名称
        """
        print("🔍 正在从 DiffPair 节点提取差分对 (优先级 3)...")

        diff_pair_sections = self.find_xml_objects_sections(root, 'DiffPair')
        print(f"   找到 {len(diff_pair_sections)} 个 DiffPair 节点")

        for section in diff_pair_sections:
            root_nodes = self.get_direct_children(section, 'root')

            for root_node in root_nodes:
                objects = self.get_direct_children(root_node, 'object')
                print(f"   找到 {len(objects)} 个 object")

                for obj in objects:
                    info = self.extract_object_info(obj)
                    if info is None:
                        continue

                    if len(info['members']) >= 2:
                        pair_info = {
                            "pair_name": info['object_name'],
                            "nets": info['members'],
                            "impedance": info['impedance_value'],
                            "unit": "Ω",
                            "impedance_rule": info['impedance_ref']
                        }
                        self.differential.append(pair_info)
                        print(f"   ✓ 差分对：{info['object_name']} -> {info['members']} ({info['impedance_value']}Ω)")

                        # ⚠️ 关键：记录所有属于差分对的网络
                        for net_name in info['members']:
                            if net_name:
                                self.all_diff_pair_nets.add(net_name)

        print(f"   ℹ️ 共发现 {len(self.all_diff_pair_nets)} 个网络属于差分对")
        return len(self.differential)

    def build_single_ended_list(self):
        """
        构建单端信号列表
        判断网络是否属于差分对：查看是否在 all_diff_pair_nets 集合中
        """
        print("🔍 正在构建单端信号列表...")

        # 1. 添加 Net/Xnet 中定义的网络
        for net_name, info in self.impedance_priority_map.items():
            # ⚠️ 关键：根据 all_diff_pair_nets 判断，而不是 impedance_source
            is_from_diff_pair = net_name in self.all_diff_pair_nets

            net_info = {
                "net_name": net_name,
                "impedance": info['impedance'],
                "unit": "Ω",
                "impedance_rule": info['impedance_rule'],
                "impedance_source": info['source'],
                "priority": info['priority'],
                "from_diff_pair": is_from_diff_pair  # 正确标记
            }
            self.single_ended.append(net_info)

        # 2. 添加差分对中的网络（如果未在 Net/Xnet 中定义）
        added_count = 0
        existing_nets = set(self.impedance_priority_map.keys())

        for pair in self.differential:
            for net_name in pair['nets']:
                if net_name and net_name not in existing_nets:
                    # ⚠️ 这些网络一定来自差分对
                    net_info = {
                        "net_name": net_name,
                        "impedance": pair['impedance'],
                        "unit": "Ω",
                        "impedance_rule": pair['impedance_rule'],
                        "impedance_source": "DiffPair",
                        "priority": 3,
                        "from_diff_pair": True  # 明确标记
                    }
                    self.single_ended.append(net_info)
                    existing_nets.add(net_name)
                    added_count += 1
                    print(f"   ✓ 添加差分网络：{net_name} ({pair['impedance']}Ω)")

        # 按网络名排序
        self.single_ended.sort(key=lambda x: x['net_name'])

        # 统计
        diff_count = sum(1 for net in self.single_ended if net.get('from_diff_pair'))
        pure_count = len(self.single_ended) - diff_count
        print(f"   ✓ 构建 {len(self.single_ended)} 条单端信号记录")
        print(f"      - 属于差分对：{diff_count} 条")
        print(f"      - 纯正单端线：{pure_count} 条")

        return len(self.single_ended)

    def trim_results(self):
        """
        执行裁剪逻辑
        核心规则：
        1. 用 all_diff_pair_nets 判断网络是否属于任何差分对（用于分类纯正单端线）
        2. 用 trimmed_diff_nets 判断网络是否属于保留的差分对（用于最终标记）
        3. 最终输出 = 保留的差分对网络 + 采样的纯正单端线
        """
        if not self.enable_trimming:
            self.trimmed_differential = self.differential.copy()
            self.trimmed_single_ended = self.single_ended.copy()
            print("ℹ️  裁剪功能未启用，保留全部线网")
            return

        print("\n✂️  正在执行线网裁剪...")
        print(f"   目标：差分对={self.trim_diff_pair_count}, 纯正单端线={self.trim_single_ended_count}")

        import random

        # ========================================
        # 步骤 1: 裁剪差分对
        # ========================================
        actual_diff_count = len(self.differential)
        if self.trim_diff_pair_count <= 0 or self.trim_diff_pair_count >= actual_diff_count:
            self.trimmed_differential = self.differential.copy()
            print(f"   ✓ 差分对：保留全部 {actual_diff_count} 组")
        else:
            self.trimmed_differential = random.sample(self.differential, self.trim_diff_pair_count)
            print(f"   ✓ 差分对：从 {actual_diff_count} 组中采样 {self.trim_diff_pair_count} 组")

        # ========================================
        # 步骤 2: 收集保留的差分对中的网络
        # ========================================
        trimmed_diff_nets = set()
        for pair in self.trimmed_differential:
            for net_name in pair.get('nets', []):
                if net_name:
                    trimmed_diff_nets.add(net_name)

        print(f"   ℹ️ 保留的差分对中包含 {len(trimmed_diff_nets)} 个网络")
        print(f"   ℹ️ 所有差分对中包含 {len(self.all_diff_pair_nets)} 个网络（用于判断纯正单端线）")

        # ========================================
        # 步骤 3: 分离单端线
        #        ⚠️ 关键：用 all_diff_pair_nets 判断是否属于任何差分对
        # ========================================
        single_ended_from_any_diff = []  # 属于任何差分对的网络
        single_ended_pure = []  # 从来不属于任何差分对的网络（用于采样）

        for net in self.single_ended:
            net_name = net.get('net_name', '')
            if net_name in self.all_diff_pair_nets:
                single_ended_from_any_diff.append(net)
            else:
                single_ended_pure.append(net)

        print(f"   ℹ️ 单端线分类：属于任何差分对={len(single_ended_from_any_diff)}, 纯正单端={len(single_ended_pure)}")

        # ========================================
        # 步骤 4: 只对纯正单端线进行采样
        # ========================================
        actual_pure_count = len(single_ended_pure)
        if self.trim_single_ended_count <= 0 or self.trim_single_ended_count >= actual_pure_count:
            trimmed_pure = single_ended_pure.copy()
            print(f"   ✓ 纯正单端线：保留全部 {actual_pure_count} 条")
        else:
            trimmed_pure = random.sample(single_ended_pure, self.trim_single_ended_count)
            print(f"   ✓ 纯正单端线：从 {actual_pure_count} 条中采样 {self.trim_single_ended_count} 条")

        # ========================================
        # 步骤 5: 从"属于任何差分对"的网络中，只保留属于"保留的差分对"的网络
        # ========================================
        single_ended_from_trimmed_diff = []
        for net in single_ended_from_any_diff:
            net_name = net.get('net_name', '')
            if net_name in trimmed_diff_nets:
                # 创建副本并设置正确的标记
                net_copy = net.copy()
                net_copy['from_diff_pair'] = True
                single_ended_from_trimmed_diff.append(net_copy)

        print(f"   ℹ️ 从差分对网络中保留 {len(single_ended_from_trimmed_diff)} 条（属于采样保留的差分对）")

        # ========================================
        # 步骤 6: 合并最终单端线列表
        # ========================================
        self.trimmed_single_ended = single_ended_from_trimmed_diff + trimmed_pure
        self.trimmed_single_ended.sort(key=lambda x: x['net_name'])

        # 为纯正单端线设置标记
        for net in trimmed_pure:
            net['from_diff_pair'] = False

        total_single = len(self.trimmed_single_ended)
        diff_count = len(single_ended_from_trimmed_diff)
        pure_count = len(trimmed_pure)
        print(f"   ✓ 最终单端线总数：{total_single} 条")
        print(f"      - 来自保留的差分对：{diff_count} 条（自动保留）")
        print(f"      - 纯正单端线：{pure_count} 条（按指定数量采样）")

    def export_json(self, output_file='impedance_requirements.json'):
        print(f"\n💾 正在导出 JSON 到 {output_file}...")

        # 使用裁剪后的结果（如果启用了裁剪）
        single_list = self.trimmed_single_ended if self.enable_trimming else self.single_ended
        diff_list = self.trimmed_differential if self.enable_trimming else self.differential

        result = {
            "trimming_info": {
                "enabled": self.enable_trimming,
                "target_diff_pairs": self.trim_diff_pair_count,
                "target_single_ended": self.trim_single_ended_count,
                "actual_diff_pairs": len(diff_list),
                "actual_single_ended": len(single_list)
            },
            "single_ended": {
                "description": "单端阻抗信号（优先级：Net > Xnet > DiffPair）",
                "count": len(single_list),
                "nets": single_list
            },
            "differential": {
                "description": "差分阻抗信号对",
                "count": len(diff_list),
                "pairs": diff_list
            },
            "summary": {
                "total_single_nets": len(single_list),
                "total_diff_pairs": len(diff_list),
                "impedance_mappings": len(self.impedance_priority_map),
                "source_file": self.dcfx_file
            }
        }

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"   ✓ JSON 导出完成")
        return output_file

    def print_summary(self):
        print("\n" + "=" * 60)
        print("📊 提取摘要")
        print("=" * 60)

        single_list = self.trimmed_single_ended if self.enable_trimming else self.single_ended
        diff_list = self.trimmed_differential if self.enable_trimming else self.differential

        if self.enable_trimming:
            print(f"✂️  裁剪功能：已启用")
            print(f"   目标差分对：{self.trim_diff_pair_count} → 实际：{len(diff_list)}")
            print(f"   目标单端线：{self.trim_single_ended_count} → 实际：{len(single_list)}")
        else:
            print(f"✂️  裁剪功能：未启用")

        print(f"✅ 阻抗优先级映射：{len(self.impedance_priority_map)}")
        print(f"✅ 单端信号数量：{len(single_list)}")
        print(f"✅ 差分对数量：{len(diff_list)}")

        # 按来源统计
        source_count = {}
        for net in single_list:
            source = net.get('impedance_source', 'Unknown')
            source_count[source] = source_count.get(source, 0) + 1

        print("\n【单端信号来源统计】")
        for source, count in sorted(source_count.items()):
            print(f"   • {source}: {count} 条")

        # ⚠️ 关键：根据 from_diff_pair 标记统计（这个标记在 trim_results 中已更新）
        from_diff_count = sum(1 for net in single_list if net.get('from_diff_pair'))
        pure_count = len(single_list) - from_diff_count
        print(f"\n【单端线构成】")
        print(f"   • 来自差分对：{from_diff_count} 条")
        print(f"   • 纯正单端线：{pure_count} 条")

        if single_list:
            print("\n【单端信号列表 - 前 15 个】")
            for i, net in enumerate(single_list[:15], 1):
                from_tag = " [差分]" if net.get('from_diff_pair') else ""
                print(f"   {i}. {net['net_name']}: {net['impedance']}Ω (来源：{net['impedance_source']}){from_tag}")
            if len(single_list) > 15:
                print(f"   ... 还有 {len(single_list) - 15} 个")

        if diff_list:
            print("\n【差分对列表 - 前 10 个】")
            for i, pair in enumerate(diff_list[:10], 1):
                print(f"   {i}. {pair['pair_name']}: {pair['nets']} ({pair['impedance']}Ω)")
            if len(diff_list) > 10:
                print(f"   ... 还有 {len(diff_list) - 10} 个")

        print("=" * 60)

    def run(self):
        print("=" * 60)
        print("🚀 Cadence 阻抗约束提取工具 - 最终修正版")
        print("=" * 60)

        if self.enable_trimming:
            print(f"⚙️  裁剪配置：差分对={self.trim_diff_pair_count}, 单端线={self.trim_single_ended_count}")

        root = self.parse_file()
        if root is None:
            return

        # 1. 从 Net 节点提取（优先级 1 - 最高）
        self.extract_net_impedance(root)

        # 2. 从 Xnet 节点提取（优先级 2）
        self.extract_xnet_impedance(root)

        # 3. 从 DiffPair 节点提取（优先级 3 - 最低）
        self.extract_diff_pairs(root)

        # 4. 构建单端信号列表
        self.build_single_ended_list()

        # 5. === 新增：执行裁剪 ===
        self.trim_results()

        # 6. 导出 JSON
        self.export_json()

        # 7. 打印摘要
        self.print_summary()


if __name__ == '__main__':
    if __name__ == '__main__':
        INPUT_FILE = "Hi3519_PCB_modified1.dcfx"

        # === 裁剪配置 ===
        ENABLE_TRIMMING = True  # 是否启用裁剪
        TRIM_DIFF_PAIR_COUNT = 2  # 目标差分对数量
        TRIM_SINGLE_COUNT = 2  # 目标纯正单端线数量

        if os.path.exists(INPUT_FILE):
            extractor = ImpedanceExtractor(
                INPUT_FILE,
                enable_trimming=ENABLE_TRIMMING,
                trim_diff_pair_count=TRIM_DIFF_PAIR_COUNT,
                trim_single_ended_count=TRIM_SINGLE_COUNT
            )
            extractor.run()
        else:
            print(f"❌ 文件不存在：{INPUT_FILE}")
