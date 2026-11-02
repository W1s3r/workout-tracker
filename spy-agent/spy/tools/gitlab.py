"""GitLab REST API (v4) tool. Local git operations go through the `shell` tool;
this covers the server side: projects, merge requests, issues, pipelines, files."""
from __future__ import annotations

import json
import urllib.parse

from spy import net
from spy.tools.base import Tool, ToolError, truncate

READ_ACTIONS = {"list_projects", "get_project", "list_mrs", "get_mr", "mr_changes", "list_issues",
                "get_issue", "list_pipelines", "pipeline_jobs", "job_log", "get_file", "list_branches", "search"}
WRITE_ACTIONS = {"create_mr", "comment_mr", "create_issue", "comment_issue", "update_issue"}


class GitLab(Tool):
    name = "gitlab"
    description = """Interact with the internal GitLab server's API. For local git (clone, commit,
push, branch, diff) use the shell tool with `git` instead.
Actions (params in brackets):
  list_projects [search]             get_project [project]
  list_branches [project]            get_file [project, path, ref]
  list_mrs [project, state]          get_mr [project, iid]      mr_changes [project, iid]
  create_mr [project, source_branch, target_branch, title, description]
  comment_mr [project, iid, body]
  list_issues [project, state, search]  get_issue [project, iid]
  create_issue [project, title, description]   comment_issue [project, iid, body]
  update_issue [project, iid, state_event(close|reopen), labels]
  list_pipelines [project, ref]  pipeline_jobs [project, pipeline_id]  job_log [project, job_id]
  search [scope(projects|issues|merge_requests|blobs), search]
`project` is "group/name" or a numeric id."""
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": sorted(READ_ACTIONS | WRITE_ACTIONS)},
        "project": {"type": "string"}, "iid": {"type": "integer"}, "search": {"type": "string"},
        "state": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
        "body": {"type": "string"}, "source_branch": {"type": "string"}, "target_branch": {"type": "string"},
        "ref": {"type": "string"}, "path": {"type": "string"}, "pipeline_id": {"type": "integer"},
        "job_id": {"type": "integer"}, "scope": {"type": "string"}, "state_event": {"type": "string"},
        "labels": {"type": "string"},
    }, "required": ["action"]}
    read_only = False

    def permission_key(self, args):
        return "gitlab(%s)" % args.get("action")

    def describe_call(self, args):
        return "gitlab %s %s" % (args.get("action"), args.get("project") or args.get("search") or "")

    def needs_permission(self, args) -> bool:
        return args.get("action") in WRITE_ACTIONS

    def _api(self, ctx, method, path, params=None, body=None):
        gcfg = ctx.config.get("gitlab") or {}
        base = (gcfg.get("url") or "").rstrip("/")
        token = gcfg.get("token") or ""
        if not base or "<" in base:
            raise ToolError("GitLab URL not configured (config gitlab.url / env SPY_GITLAB_URL).")
        url = "%s/api/v4%s" % (base, path)
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        headers = {"PRIVATE-TOKEN": token} if token and "<" not in token else {}
        cfg = {"verify_ssl": gcfg.get("verify_ssl", True), "ca_bundle": gcfg.get("ca_bundle"), "timeout": 60}
        try:
            if body is not None:
                _, _, raw = net.request(url, cfg, method, json.dumps(body).encode(),
                                        dict(headers, **{"Content-Type": "application/json"}))
            else:
                _, _, raw = net.request(url, cfg, method, None, headers)
        except net.HTTPError as e:
            raise ToolError(str(e))
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def run(self, args, ctx):
        a = args["action"]
        p = urllib.parse.quote(str(args.get("project") or ""), safe="")
        need_p = a not in ("list_projects", "search")
        if need_p and not p:
            raise ToolError("project is required for %s" % a)
        iid = args.get("iid")
        if a == "list_projects":
            r = self._api(ctx, "GET", "/projects", {"search": args.get("search"), "membership": "true",
                                                     "per_page": 50, "order_by": "last_activity_at"})
            return "\n".join("%s  (id %s)  %s" % (x["path_with_namespace"], x["id"], x.get("http_url_to_repo", ""))
                             for x in r) or "No projects."
        if a == "get_project":
            return _fmt(self._api(ctx, "GET", "/projects/%s" % p))
        if a == "list_branches":
            r = self._api(ctx, "GET", "/projects/%s/repository/branches" % p, {"per_page": 100})
            return "\n".join(b["name"] for b in r)
        if a == "get_file":
            r = self._api(ctx, "GET", "/projects/%s/repository/files/%s/raw" % (p, urllib.parse.quote(args.get("path", ""), safe="")),
                          {"ref": args.get("ref") or "HEAD"})
            return truncate(r if isinstance(r, str) else _fmt(r), 20000)
        if a == "list_mrs":
            r = self._api(ctx, "GET", "/projects/%s/merge_requests" % p, {"state": args.get("state") or "opened", "per_page": 30})
            return "\n".join("!%s %s [%s] %s -> %s by %s" % (m["iid"], m["title"], m["state"], m["source_branch"],
                                                            m["target_branch"], (m.get("author") or {}).get("username"))
                             for m in r) or "No merge requests."
        if a == "get_mr":
            return _fmt(self._api(ctx, "GET", "/projects/%s/merge_requests/%s" % (p, iid)))
        if a == "mr_changes":
            r = self._api(ctx, "GET", "/projects/%s/merge_requests/%s/changes" % (p, iid))
            return truncate("\n".join("--- %s\n%s" % (c["new_path"], c["diff"]) for c in r.get("changes", [])), 30000)
        if a == "create_mr":
            r = self._api(ctx, "POST", "/projects/%s/merge_requests" % p, body={
                "source_branch": args.get("source_branch"), "target_branch": args.get("target_branch") or "main",
                "title": args.get("title"), "description": args.get("description", "")})
            return "Created MR !%s: %s" % (r.get("iid"), r.get("web_url"))
        if a == "comment_mr":
            self._api(ctx, "POST", "/projects/%s/merge_requests/%s/notes" % (p, iid), body={"body": args.get("body")})
            return "Comment added."
        if a == "list_issues":
            r = self._api(ctx, "GET", "/projects/%s/issues" % p, {"state": args.get("state") or "opened",
                                                                   "search": args.get("search"), "per_page": 30})
            return "\n".join("#%s %s [%s] %s" % (i["iid"], i["title"], i["state"], ",".join(i.get("labels", [])))
                             for i in r) or "No issues."
        if a == "get_issue":
            r = self._api(ctx, "GET", "/projects/%s/issues/%s" % (p, iid))
            notes = self._api(ctx, "GET", "/projects/%s/issues/%s/notes" % (p, iid), {"per_page": 50})
            return _fmt(r) + "\n\nComments:\n" + "\n".join("- %s: %s" % ((n.get("author") or {}).get("username"), n.get("body"))
                                                         for n in (notes if isinstance(notes, list) else []))
        if a == "create_issue":
            r = self._api(ctx, "POST", "/projects/%s/issues" % p, body={"title": args.get("title"),
                                                                         "description": args.get("description", "")})
            return "Created issue #%s: %s" % (r.get("iid"), r.get("web_url"))
        if a == "comment_issue":
            self._api(ctx, "POST", "/projects/%s/issues/%s/notes" % (p, iid), body={"body": args.get("body")})
            return "Comment added."
        if a == "update_issue":
            body = {k: args[k] for k in ("state_event", "labels", "title", "description") if args.get(k)}
            self._api(ctx, "PUT", "/projects/%s/issues/%s" % (p, iid), body=body)
            return "Issue updated."
        if a == "list_pipelines":
            r = self._api(ctx, "GET", "/projects/%s/pipelines" % p, {"ref": args.get("ref"), "per_page": 20})
            return "\n".join("%s %s %s %s" % (x["id"], x["status"], x["ref"], x.get("created_at", "")) for x in r)
        if a == "pipeline_jobs":
            r = self._api(ctx, "GET", "/projects/%s/pipelines/%s/jobs" % (p, args.get("pipeline_id")))
            return "\n".join("%s %s %s %s" % (j["id"], j["name"], j["stage"], j["status"]) for j in r)
        if a == "job_log":
            r = self._api(ctx, "GET", "/projects/%s/jobs/%s/trace" % (p, args.get("job_id")))
            return truncate(r if isinstance(r, str) else _fmt(r), 20000)
        if a == "search":
            r = self._api(ctx, "GET", "/search", {"scope": args.get("scope") or "projects", "search": args.get("search")})
            return truncate(_fmt(r), 15000)
        raise ToolError("Unknown action %s" % a)


def _fmt(obj) -> str:
    if isinstance(obj, dict):
        keep = {k: v for k, v in obj.items() if not isinstance(v, (dict, list)) or k in ("labels",)}
        return "\n".join("%s: %s" % (k, v) for k, v in keep.items())
    return json.dumps(obj, indent=1)[:15000]


TOOLS = [GitLab]
