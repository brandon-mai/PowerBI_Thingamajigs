import os
import json
import re
import sys

# Regex patterns for finding usage in JSON strings
ENTITY_PROP_PATTERN = re.compile(r'"SourceRef":\s*\{\s*"Entity":\s*"(.*?)"\s*\},\s*"Property":\s*"(.*?)"')
# This one is for queries where the entity might be aliased
QUERY_REF_PATTERN = re.compile(r'"queryRef":\s*"(.*?)\.(.*?)"')

class PBIPruner:
    def __init__(self, root_path, dry_run=True):
        self.root_path = os.path.abspath(root_path)
        self.dry_run = dry_run
        self.used_fields = set()  # Set of (table, field)
        self.potential_fields = set() # Set of field names
        self.model_info = {}      # Table -> {'columns': set(), 'measures': set(), 'file_path': str}
        self.dependencies = {}    # (table, field) -> set of (table, field)
        self.report_path = ""
        self.semantic_model_path = ""

    def log(self, msg):
        print(f"[INFO] {msg}")

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

        # Use a regex that handles escaped quotes correctly
        string_pattern = re.compile(r'"((?:[^"\\]|\\.)*)"')

        for root, _, files in os.walk(definition_path):
            for file in files:
                if file.endswith(('.json', '.pbir', '.visual.json', '.page.json')):
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            
                            # Initial scan
                            matches = string_pattern.findall(content)
                            for m in matches:
                                # Clean the match (remove backslashes from escaped quotes)
                                clean_m = m.replace('\\"', '"').replace('\\\\', '\\')
                                self.potential_fields.add(clean_m)
                                
                                # If it looks like nested JSON, scan it again
                                if '{' in clean_m and ':' in clean_m:
                                    inner_matches = string_pattern.findall(clean_m)
                                    for im in inner_matches:
                                        self.potential_fields.add(im.replace('\\"', '"'))
                            
                            # Also find dot-notated references Table.Field
                            for match in re.findall(r'([\w\s.2_-]+)\.([\w\s.2_-]+)', content):
                                self.used_fields.add((match[0].strip(), match[1].strip()))
                    except: pass
        
        self.log(f"Found {len(self.potential_fields)} unique strings in report.")

    def scan_model_metadata(self):
        self.log(f"Scanning model metadata structural links...")
        
        # 1. Discover all tables and fields first (need them for resolution)
        tables_path = os.path.join(self.semantic_model_path, "definition", "tables")
        if os.path.isdir(tables_path):
            for file in os.listdir(tables_path):
                if file.endswith(".tmdl"):
                    self._parse_table_tmdl(os.path.join(tables_path, file))

        # 2. Relationships (must be kept to maintain model structure)
        rel_path = os.path.join(self.semantic_model_path, "definition", "relationships.tmdl")
        if os.path.exists(rel_path):
            with open(rel_path, 'r', encoding='utf-8') as f:
                for line in f:
                    match = re.search(r'(?:from|to)Column:\s*(.*?)\.(.*)', line)
                    if match:
                        t_name = match.group(1).strip().strip("'")
                        f_name = match.group(2).strip().strip("'")
                        self.used_fields.add((t_name, f_name))

    def _parse_table_tmdl(self, file_path):
        table_name = ""
        current_field = None
        
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                t_match = re.match(r'^table\s+\'?([^\'\r\n]+)\'?', line)
                if t_match:
                    table_name = t_match.group(1).strip()
                    self.model_info[table_name] = {'columns': set(), 'measures': set(), 'file_path': file_path}
                    continue
                if not table_name: continue

                stripped = line.strip()
                if not stripped: continue
                
                # Identify Column, Measure, or Hierarchy
                if stripped.startswith("column "):
                    # Column names stop at the first space or = if they are not quoted
                    f_match = re.search(r'column\s+\'?([^\'=\r\n]+)\'?', stripped)
                    if f_match:
                        current_field = f_match.group(1).split('=')[0].strip().strip("'")
                        self.model_info[table_name]['columns'].add(current_field)
                elif stripped.startswith("measure "):
                    f_match = re.search(r'measure\s+\'?([^\'=\r\n]+)\'?\s*=', stripped)
                    if f_match:
                        current_field = f_match.group(1).strip().strip("'")
                        self.model_info[table_name]['measures'].add(current_field)
                elif stripped.startswith("hierarchy "):
                    f_match = re.search(r'hierarchy\s+\'?([^\'\r\n]+)\'?', stripped)
                    if f_match:
                        current_field = f_match.group(1).strip().strip("'")
                        self.model_info[table_name]['measures'].add(current_field)
                
                if current_field:
                    key = (table_name, current_field)
                    if key not in self.dependencies: self.dependencies[key] = set()
                    
                    # VARIATIONS (Critical for Date Hierarchies)
                    v_match = re.search(r'defaultHierarchy:\s*(.*?)\.(.*)', line)
                    if v_match:
                        t_ref = v_match.group(1).strip().strip("'")
                        f_ref = v_match.group(2).strip().strip("'")
                        self.dependencies[key].add((t_ref, f_ref))

                    # Look for sortByColumn
                    s_match = re.search(r'sortByColumn:\s+\'?([^\'\r\n]+)\'?', line)
                    if s_match:
                        self.dependencies[key].add((table_name, s_match.group(1).strip().strip("'")))
                    
                    # Look for Hierarchy levels
                    hl_match = re.search(r'^\s+column:\s+\'?([^\'\r\n]+)\'?', line)
                    if hl_match:
                        self.dependencies[key].add((table_name, hl_match.group(1).strip().strip("'")))
                    
                    # Robust DAX reference detection
                    # Table names can have special characters if quoted, or be simple words
                    # We look for: 'Table'[Field] or Table[Field]
                    # Table part: ([^'\[\]]+?)
                    # Field part: ([^'\[\]]+?)
                    
                    # 1. Table[Field] or 'Table'[Field]
                    for t_ref, f_ref in re.findall(r'(?<![\w\'\]])\'?([^\'\[\]\(\)\s=,]+?)\'?\[\'?([^\'\[\]]+?)\'?\]', line):
                        self.dependencies[key].add((t_ref.strip().strip("'"), f_ref.strip().strip("'")))
                    
                    # 2. [Field] (same table)
                    for f_ref in re.findall(r'(?<![\w\'\]])\[\'?([^\'\[\]]+?)\'?\]', line):
                        self.dependencies[key].add((table_name, f_ref.strip().strip("'")))
                    
                    # 3. Look for Semantic Links in annotations (JSON)
                    for t_ref, f_ref in re.findall(r'"TableName":\s*"(.*?)".*?"TableItemName":\s*"(.*?)"', line):
                        self.dependencies[key].add((t_ref.strip().strip("'"), f_ref.strip().strip("'")))

    def trace_dependencies(self):
        # 1. Resolve potential_fields into used_fields
        if self.potential_fields:
            self.log(f"Resolving {len(self.potential_fields)} potential fields...")
            for table_name, info in self.model_info.items():
                for pf in self.potential_fields:
                    if pf in info['columns'] or pf in info['measures']:
                        self.used_fields.add((table_name, pf))

        # 2. Structural Protection: Relationship columns and System tables
        self.log("Applying structural protection rules...")
        for table_name, info in self.model_info.items():
            # Never prune from system tables (Auto Date/Time)
            if table_name.startswith("LocalDateTable_") or table_name.startswith("DateTableTemplate_"):
                for f in (info['columns'] | info['measures']):
                    self.used_fields.add((table_name, f))

        self.log(f"Tracing dependencies for {len(self.used_fields)} fields...")
        
        # 3. Iterative trace (DAX, Hierarchies, Variations)
        changed = True
        while changed:
            count_before = len(self.used_fields)
            for table_name, info in self.model_info.items():
                for field in (info['columns'] | info['measures']):
                    key = (table_name, field)
                    if key in self.used_fields and key in self.dependencies:
                        for dep_key in self.dependencies[key]:
                            if dep_key not in self.used_fields:
                                t, f = dep_key
                                if t in self.model_info and (f in self.model_info[t]['columns'] or f in self.model_info[t]['measures']):
                                    self.used_fields.add(dep_key)
            changed = len(self.used_fields) > count_before

        self.log(f"Final used fields identified: {len(self.used_fields)}")

    def prune(self):
        self.log(f"Total used fields identified: {len(self.used_fields)}")
        for table_name, info in self.model_info.items():
            used_in_table = [f for t, f in self.used_fields if t == table_name]
            # We never delete system tables
            is_system = table_name.startswith("LocalDateTable_") or table_name.startswith("DateTableTemplate_")
            
            if not used_in_table and not is_system:
                self.log(f"Deleting unused table: {table_name}")
                if not self.dry_run:
                    os.remove(info['file_path'])
                    self._remove_table_ref(table_name)
            else:
                self._prune_table_file(info['file_path'], table_name)

    def _prune_table_file(self, file_path, table_name):
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        skip_until_indent = -1
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if skip_until_indent == -1: new_lines.append(line)
                continue

            indent = len(line) - len(line.lstrip())
            
            if skip_until_indent != -1:
                if indent > skip_until_indent: continue
                else: skip_until_indent = -1

            # Use robust detection for pruning
            c_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
            m_match = re.search(r'^\s+measure\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            h_match = re.search(r'^\s+hierarchy\s+\'?([^\'\r\n]+)\'?', line)
            
            field_name = None
            if c_match: field_name = c_match.group(1).split('=')[0].strip().strip("'")
            elif m_match: field_name = m_match.group(1).strip().strip("'")
            elif h_match: field_name = h_match.group(1).strip().strip("'")
            
            if field_name:
                if (table_name, field_name) not in self.used_fields:
                    self.log(f"Pruning: {table_name}[{field_name}]")
                    skip_until_indent = indent
                    continue
            
            new_lines.append(line)

        if not self.dry_run:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)

    def _remove_table_ref(self, table_name):
        model_tmdl = os.path.join(self.semantic_model_path, "definition", "model.tmdl")
        if not os.path.exists(model_tmdl): return
        with open(model_tmdl, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = [l for l in lines if not (l.strip().startswith("ref table") and (f"'{table_name}'" in l or f" {table_name}" in l))]
        with open(model_tmdl, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

    def run(self):
        self.find_paths()
        if not self.report_path or not self.semantic_model_path: return
        self.scan_report_usage()
        self.scan_model_metadata()
        self.trace_dependencies()
        self.prune()
        self.log("Done.")

    def run(self):
        self.find_paths()
        if not self.report_path or not self.semantic_model_path:
            print("Error: Could not find .Report or .SemanticModel folders.")
            return

        self.scan_report_usage()
        self.scan_model_metadata()
        self.trace_dependencies()
        
        self.log(f"Total used fields found: {len(self.used_fields)}")
        self.prune()
        self.log("Done.")

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    dry = "--run" not in sys.argv # Default to dry run
    pruner = PBIPruner(path, dry_run=dry)
    if dry:
        print("!!! DRY RUN MODE - No files will be modified. Use --run to apply changes. !!!")
    pruner.run()
