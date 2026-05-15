import os
import json
import re
import sys

class PBIPruner:
    def __init__(self, root_path, dry_run=True):
        self.root_path = os.path.abspath(root_path)
        self.dry_run = dry_run
        self.used_fields = set()  # Set of (table, field)
        self.potential_fields = set() # Set of field names
        self.model_info = {}      # Table -> {'columns': set(), 'measures': set(), 'file_path': str}
        self.dependencies = {}    # (table, field) -> set of (table, field)
        self.relationship_blocks = []
        self.report_path = ""
        self.semantic_model_path = ""

    def log(self, msg):
        print(f"\033[34m[INFO]\033[0m {msg}")

    def find_paths(self):
        for item in os.listdir(self.root_path):
            full_path = os.path.join(self.root_path, item)
            if os.path.isdir(full_path):
                if item.endswith(".Report"):
                    self.report_path = full_path
                elif item.endswith(".SemanticModel"):
                    self.semantic_model_path = full_path

    def scan_report_usage(self):
        self.log(f"Scanning report usage in {self.report_path}...")
        definition_path = os.path.join(self.report_path, "definition")
        if not os.path.isdir(definition_path):
            definition_path = self.report_path

        string_pattern = re.compile(r'"((?:[^"\\]|\\.)*)"')

        for root, _, files in os.walk(definition_path):
            for file in files:
                if file.endswith(('.json', '.pbir', '.visual.json', '.page.json')):
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            matches = string_pattern.findall(content)
                            for m in matches:
                                clean_m = m.replace('\\"', '"').replace('\\\\', '\\')
                                self.potential_fields.add(clean_m)
                                if '{' in clean_m and ':' in clean_m:
                                    inner_matches = string_pattern.findall(clean_m)
                                    for im in inner_matches:
                                        self.potential_fields.add(im.replace('\\"', '"'))
                            
                            for match in re.findall(r'([\w\s.2_-]+)\.([\w\s.2_-]+)', content):
                                self.used_fields.add((match[0].strip(), match[1].strip()))
                    except: pass
        self.log(f"Found {len(self.potential_fields)} unique strings in report.")

    def scan_model_metadata(self):
        self.log(f"Scanning model metadata structural links...")
        tables_path = os.path.join(self.semantic_model_path, "definition", "tables")
        if os.path.isdir(tables_path):
            for file in os.listdir(tables_path):
                if file.endswith(".tmdl"):
                    self._parse_table_tmdl(os.path.join(tables_path, file))

        rel_path = os.path.join(self.semantic_model_path, "definition", "relationships.tmdl")
        if os.path.exists(rel_path):
            with open(rel_path, 'r', encoding='utf-8') as f:
                content = f.read()
                blocks = content.split('\nrelationship ')
                for i, block in enumerate(blocks):
                    # Clean up the block to find the start of the relationship keyword
                    clean_block = block.strip()
                    if not clean_block: continue
                    
                    if i == 0 and not content.startswith('relationship'):
                        # This is probably a header (like 'section model')
                        continue
                    
                    full_block = 'relationship ' + clean_block if not clean_block.startswith('relationship') else clean_block
                    
                    f_match = re.search(r'fromColumn:\s*(.*?)\.(.*)', full_block)
                    t_match = re.search(r'toColumn:\s*(.*?)\.(.*)', full_block)
                    if f_match and t_match:
                        from_t, from_f = f_match.group(1).strip().strip("'"), f_match.group(2).strip().strip("'")
                        to_t, to_f = t_match.group(1).strip().strip("'"), t_match.group(2).strip().strip("'")
                        rel_id_match = re.search(r'relationship\s+([\w-]+)', full_block)
                        if rel_id_match:
                            rel_info = {'id': rel_id_match.group(1), 'from': (from_t, from_f), 'to': (to_t, to_f), 'raw': full_block}
                            self.relationship_blocks.append(rel_info)
                            key_f, key_t = (from_t, from_f), (to_t, to_f)
                            if key_f not in self.dependencies: self.dependencies[key_f] = set()
                            if key_t not in self.dependencies: self.dependencies[key_t] = set()
                            self.dependencies[key_f].add(key_t)
                            self.dependencies[key_t].add(key_f)

    def _parse_table_tmdl(self, file_path):
        table_name = ""
        current_field = None
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                t_match = re.match(r'^table\s+\'?([^\'\r\n]+)\'?', line)
                if t_match:
                    table_name = t_match.group(1).strip()
                    self.model_info[table_name] = {'columns': set(), 'measures': set(), 'file_path': file_path}
                    continue
                if not table_name: continue
                stripped = line.strip()
                if stripped.startswith("column "):
                    f_match = re.search(r'column\s+\'?([^\'=\r\n]+)\'?', stripped)
                    if f_match: current_field = f_match.group(1).split('=')[0].strip().strip("'"); self.model_info[table_name]['columns'].add(current_field)
                elif stripped.startswith("measure "):
                    f_match = re.search(r'measure\s+\'?([^\'=\r\n]+)\'?\s*=', stripped)
                    if f_match: current_field = f_match.group(1).strip().strip("'"); self.model_info[table_name]['measures'].add(current_field)
                elif stripped.startswith("hierarchy "):
                    f_match = re.search(r'hierarchy\s+\'?([^\'\r\n]+)\'?', stripped)
                    if f_match: current_field = f_match.group(1).strip().strip("'"); self.model_info[table_name]['measures'].add(current_field)
                
                if current_field:
                    key = (table_name, current_field)
                    if key not in self.dependencies: self.dependencies[key] = set()
                    v_match = re.search(r'defaultHierarchy:\s*(.*?)\.(.*)', line)
                    if v_match: self.dependencies[key].add((v_match.group(1).strip().strip("'"), v_match.group(2).strip().strip("'")))
                    s_match = re.search(r'sortByColumn:\s+\'?([^\'\r\n]+)\'?', line)
                    if s_match: self.dependencies[key].add((table_name, s_match.group(1).strip().strip("'")))
                    hl_match = re.search(r'^\s+column:\s+\'?([^\'\r\n]+)\'?', line)
                    if hl_match: self.dependencies[key].add((table_name, hl_match.group(1).strip().strip("'")))
                    for t_ref, f_ref in re.findall(r'(?<![\w\'\]])\'?([^\'\[\]\(\)\s=,]+?)\'?\[\'?([^\'\[\]]+?)\'?\]', line):
                        self.dependencies[key].add((t_ref.strip().strip("'"), f_ref.strip().strip("'")))
                    for f_ref in re.findall(r'(?<![\w\'\]])\[\'?([^\'\[\]]+?)\'?\]', line):
                        self.dependencies[key].add((table_name, f_ref.strip().strip("'")))
                    for t_ref, f_ref in re.findall(r'"TableName":\s*"(.*?)".*?"TableItemName":\s*"(.*?)"', line):
                        self.dependencies[key].add((t_ref.strip().strip("'"), f_ref.strip().strip("'")))

    def trace_dependencies(self):
        measure_map = {m: t for t, info in self.model_info.items() for m in info['measures']}
        
        # 1. Resolve initial used fields from visuals
        if self.potential_fields:
            for t_name, info in self.model_info.items():
                for pf in self.potential_fields:
                    if pf in info['columns'] or pf in info['measures']: self.used_fields.add((t_name, pf))
        
        # 2. System table protection
        for t_name, info in self.model_info.items():
            if t_name.startswith(("LocalDateTable_", "DateTableTemplate_")):
                for f in (info['columns'] | info['measures']): self.used_fields.add((t_name, f))

        # 3. Iterative Tracing (DAX + Relationships)
        changed = True
        while changed:
            count_before = len(self.used_fields)
            
            # A. Identify which tables are currently "Used"
            used_tables = {t for t, f in self.used_fields}
            
            # B. Protect Relationships between used tables
            for rel in self.relationship_blocks:
                # If both tables are used, the relationship is mandatory
                if rel['from'][0] in used_tables and rel['to'][0] in used_tables:
                    if rel['from'] not in self.used_fields: self.used_fields.add(rel['from'])
                    if rel['to'] not in self.used_fields: self.used_fields.add(rel['to'])

            # C. Trace DAX and other dependencies
            for t_name, info in self.model_info.items():
                for field in (info['columns'] | info['measures']):
                    key = (t_name, field)
                    if key in self.used_fields and key in self.dependencies:
                        for dep_key in self.dependencies[key]:
                            t, f = dep_key
                            if t in self.model_info and (f in self.model_info[t]['columns'] or f in self.model_info[t]['measures']):
                                if dep_key not in self.used_fields: self.used_fields.add(dep_key)
                            elif f in measure_map:
                                g_dep = (measure_map[f], f)
                                if g_dep not in self.used_fields: self.used_fields.add(g_dep)
            
            changed = len(self.used_fields) > count_before
        self.log(f"Final used fields identified: {len(self.used_fields)}")

    def prune(self):
        used_rel_ids = {rel['id'] for rel in self.relationship_blocks if rel['from'] in self.used_fields and rel['to'] in self.used_fields}
        self._prune_relationships(used_rel_ids)
        for table_name, info in self.model_info.items():
            used_in_table = [f for t, f in self.used_fields if t == table_name]
            if not used_in_table and not table_name.startswith(("LocalDateTable_", "DateTableTemplate_")):
                self.log(f"Deleting unused table: {table_name}")
                if not self.dry_run:
                    os.remove(info['file_path'])
                    self._remove_table_ref(table_name)
            else:
                self._prune_table_file(info['file_path'], table_name)

    def _prune_relationships(self, used_rel_ids):
        rel_path = os.path.join(self.semantic_model_path, "definition", "relationships.tmdl")
        if not os.path.exists(rel_path): return
        new_blocks = [rel['raw'] for rel in self.relationship_blocks if rel['id'] in used_rel_ids]
        if not self.dry_run:
            with open(rel_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(new_blocks))
        else:
            for rel in self.relationship_blocks:
                if rel['id'] not in used_rel_ids: self.log(f"Pruning Relationship: {rel['from'][0]} -> {rel['to'][0]}")

    def _prune_table_file(self, file_path, table_name):
        with open(file_path, 'r', encoding='utf-8') as f: lines = f.readlines()
        new_lines, skip_until_indent = [], -1
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if skip_until_indent == -1: new_lines.append(line)
                continue
            indent = len(line) - len(line.lstrip())
            if skip_until_indent != -1:
                if indent > skip_until_indent: continue
                else: skip_until_indent = -1
            c_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
            m_match = re.search(r'^\s+measure\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            h_match = re.search(r'^\s+hierarchy\s+\'?([^\'\r\n]+)\'?', line)
            field_name = None
            if c_match: field_name = c_match.group(1).split('=')[0].strip().strip("'")
            elif m_match: field_name = m_match.group(1).strip().strip("'")
            elif h_match: field_name = h_match.group(1).strip().strip("'")
            if field_name and (table_name, field_name) not in self.used_fields:
                self.log(f"Pruning: {table_name}[{field_name}]"); skip_until_indent = indent; continue
            new_lines.append(line)
        if not self.dry_run:
            with open(file_path, 'w', encoding='utf-8') as f: f.writelines(new_lines)

    def _remove_table_ref(self, table_name):
        model_tmdl = os.path.join(self.semantic_model_path, "definition", "model.tmdl")
        if not os.path.exists(model_tmdl): return
        with open(model_tmdl, 'r', encoding='utf-8') as f: lines = f.readlines()
        new_lines = [l for l in lines if not (l.strip().startswith("ref table") and (f"'{table_name}'" in l or f" {table_name}" in l))]
        with open(model_tmdl, 'w', encoding='utf-8') as f: f.writelines(new_lines)

    def run(self):
        self.find_paths()
        if not self.report_path or not self.semantic_model_path: return
        self.scan_report_usage()
        self.scan_model_metadata()
        self.trace_dependencies()
        self.prune()
        self.log("Done.")

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    dry = "--run" not in sys.argv
    if dry: 
        print("=== DRY RUN MODE ===")
        print("To apply changes, use --run flag.")
    PBIPruner(path, dry_run=dry).run()
