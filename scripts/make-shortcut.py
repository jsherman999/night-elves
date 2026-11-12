#!/usr/bin/env python3
"""Build "Send to Elves", a share-sheet Shortcut that queues a task with the shared images or
files, then sign it so it can be imported.

    scripts/make-shortcut.py https://<studio>.ts.net [out.shortcut]

The token is not in the file: Shortcuts asks for it once, when the shortcut is imported, so the
file can be passed around and signing (which goes through Apple) never sees it. Signing needs
macOS with an iCloud account; open the result to import it, and iCloud syncs it to the phone.
"""
from __future__ import annotations
import plistlib, subprocess, sys, tempfile, uuid
from pathlib import Path

OBJ = "￼"  # where a variable sits inside a text field

def out(uid: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": uid, "OutputName": name}

def text(*parts) -> dict:
    """A text field made of plain strings and variables (dicts from out())."""
    s, refs = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            refs[f"{{{len(s)}, 1}}"] = p
            s += OBJ
    return {"Value": {"string": s, "attachmentsByRange": refs},
            "WFSerializationType": "WFTextTokenString"}

def var(ref: dict) -> dict:
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}

def file_value(ref: dict) -> dict:
    """A form field's file value: the variable wrapped one level deeper than a text value.
    Without the wrapper Shortcuts silently leaves the field out of the request."""
    return {"Value": var(ref), "WFSerializationType": "WFTokenAttachmentParameterState"}

def fields(*items) -> dict:
    """A dictionary of fields; each item is (key, type, value): type 0 is text, 5 a file."""
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": t, "WFKey": text(k), "WFValue": v} for k, t, v in items]},
        "WFSerializationType": "WFDictionaryFieldValue"}

def action(ident: str, uid: str | None = None, **params) -> dict:
    if uid:
        params["UUID"] = uid
    return {"WFWorkflowActionIdentifier": f"is.workflow.actions.{ident}",
            "WFWorkflowActionParameters": params}

def build(url: str) -> dict:
    tok, ask, lst, pick, post, msg = (str(uuid.uuid4()).upper() for _ in range(6))
    shared = {"Type": "ExtensionInput"}
    actions = [
        action("comment", WFCommentActionText=(
            "Queues a night-elves task with whatever you share: screenshots, photos, files. "
            "The token below was asked for when you imported this shortcut.")),
        action("gettext", tok, WFTextActionText=""),  # filled in by the import question
        action("ask", ask, WFAskActionPrompt="What should the elves do?", WFInputType="Text"),
        action("list", lst, WFItems=["agent", "auto", "frontier"]),
        action("choosefromlist", pick, WFInput=var(out(lst, "List")),
               WFChooseFromListActionPrompt="Tier (agent builds things and handles big files)"),
        action("downloadurl", post, WFURL=f"{url.rstrip('/')}/tasks", WFHTTPMethod="POST",
               ShowHeaders=True,
               WFHTTPHeaders=fields(("Authorization", 0, text("Bearer ", out(tok, "Text")))),
               WFHTTPBodyType="Form",
               WFFormValues=fields(("prompt", 0, text(out(ask, "Provided Input"))),
                                   ("tier", 0, text(out(pick, "Chosen Item"))),
                                   ("files", 5, file_value(shared)))),
        action("getvalueforkey", msg, WFGetDictionaryValueType="Value",
               WFDictionaryKey="message", WFInput=var(out(post, "Contents of URL"))),
        action("notification", WFNotificationActionTitle="Elves",
               WFNotificationActionBody=text(out(msg, "Dictionary Value"))),
    ]
    return {
        "WFWorkflowName": "Send to Elves",
        "WFWorkflowClientVersion": "2700",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 2071128575, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFImageContentItem", "WFGenericFileContentItem",
                                              "WFPDFContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorAskForInput",
                                      "Parameters": {"ItemClass": "WFPhotoMediaContentItem"}},
        "WFWorkflowImportQuestions": [{
            "ActionIndex": 1, "Category": "Parameter", "ParameterKey": "WFTextActionText",
            "DefaultValue": "", "Text": "Paste your ELVES_TOKEN (from ~/.night-elves/env on the Studio)"}],
        "WFWorkflowActions": actions,
    }

def main(argv: list[str]) -> int:
    if not argv or not argv[0].startswith("https://"):
        print(__doc__, file=sys.stderr)
        return 2
    dest = Path(argv[1] if len(argv) > 1 else "Send to Elves.shortcut")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "unsigned.shortcut"
        raw.write_bytes(plistlib.dumps(build(argv[0]), fmt=plistlib.FMT_BINARY))
        subprocess.run(["shortcuts", "sign", "--mode", "people-who-know-me",
                        "--input", str(raw), "--output", str(dest)], check=True)
    print(f"wrote {dest}; open it to import")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
