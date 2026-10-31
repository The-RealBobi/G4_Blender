"""Build the installable add-on from version-controlled runtime files."""
import ast
from pathlib import Path
import stat
import zipfile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    module = ast.parse((root / '__init__.py').read_text(encoding='utf-8'))
    metadata = next(ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'bl_info' for target in node.targets))
    version = '.'.join(map(str, metadata['version']))
    files = []
    excluded_roots = {'tests', 'docs', 'tools', '.cache', 'exports', 'dist', '.git', '.boss', '__pycache__'}
    for source in root.rglob('*'):
        if not source.is_file():
            continue
        path = source.relative_to(root)
        if any(part in excluded_roots or part.startswith('.') for part in path.parts):
            continue
        name = path.as_posix()
        if path.suffix in ('.py', '.json') and name != 'tools/build_release.py':
            files.append(path)
        elif name in {'README.md', 'LICENSE.md'} or (
            path.parts[0] == 'img' and path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}
        ):
            files.append(path)
    if not files or any(path.parts[0] in excluded_roots for path in files):
        raise ValueError('Unexpected files in the distribution manifest')
    required = {
        '__init__.py', 'README.md', 'animation/g4_animation_addon.py',
        'porting/g4_port_addon.py', 'data/chara_model_lookup.json',
    }
    if not required.issubset({path.as_posix() for path in files}):
        raise ValueError('The distribution manifest is missing required add-on files')
    destination = root / 'dist' / f'G4_Blender_v{version}.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        directory = zipfile.ZipInfo('G4_Blender/')
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
        directory.compress_type = zipfile.ZIP_STORED
        archive.writestr(directory, b'')
        for path in sorted(files):
            info = zipfile.ZipInfo(f'G4_Blender/{path.as_posix()}', (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / path).read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError('Distribution integrity check failed')
        if 'G4_Blender/' not in archive.namelist() or 'G4_Blender/__init__.py' not in archive.namelist():
            raise ValueError('The install ZIP must include its package directory and entry point')
    print(f'{destination.name}: {len(files)} runtime files')


if __name__ == '__main__':
    main()
