"""Package vscode-extension/ into a .vsix with this folder's config.json path baked in.

Usage: python build_vsix.py  ->  dist/agent-doorbell-helper.vsix
"""

import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "vscode-extension"
OUT = HERE / "dist" / "agent-doorbell-helper.vsix"

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="ui"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def build() -> Path:
    pkg = json.loads((SRC / "package.json").read_text(encoding="utf-8"))
    js = (SRC / "extension.js").read_text(encoding="utf-8")
    js = js.replace("__CONFIG_PATH__", json.dumps(str(HERE / "config.json")))
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", MANIFEST.format(
            name=pkg["name"], version=pkg["version"], publisher=pkg["publisher"], display=pkg["displayName"],
            description=pkg["description"], engine=pkg["engines"]["vscode"]))
        z.writestr("extension/package.json", json.dumps(pkg, indent=2))
        z.writestr("extension/extension.js", js)
    return OUT


if __name__ == "__main__":
    print(build())
