"""Build the installable add-on from version-controlled runtime files."""
import ast
from pathlib import Path
import subprocess
import zipfile


def main() -> None:
    root = Path(__file__).resolve().parent
    module = ast.parse((root / '__init__.py').read_text(encoding='utf-8'))
    metadata = next(ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'bl_info' for target in node.targets))
    version = '.'.join(map(str, metadata['version']))
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
    files = [Path(name) for name in tracked if name and Path(name).suffix in ('.py', '.json')
             and name != 'build_release.py']
    if not files or any(path.parts[0] in ('tests', 'docs', '.cache', 'exports') for path in files):
        raise ValueError('Unexpected files in the distribution manifest')
    destination = root / 'dist' / f'G4_Blender_v{version}.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(f'G4_Blender/{path.as_posix()}', (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / path).read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError('Distribution integrity check failed')
    print(f'{destination.name}: {len(files)} runtime files')


if __name__ == '__main__':
    main()
