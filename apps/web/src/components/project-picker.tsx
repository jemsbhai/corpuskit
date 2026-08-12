"use client";

import Link from "next/link";

import { useProjectContext } from "@/components/project-context";

export function ProjectPicker() {
  const context = useProjectContext();
  if (!context) return null;
  return (
    <div className="project-picker-bar">
      <div className="section-frame project-picker-inner">
        <label htmlFor="global-project-picker">Active project</label>
        {context.loading ? (
          <span role="status">Loading projects…</span>
        ) : context.projects.length ? (
          <select
            id="global-project-picker"
            onChange={(event) => context.selectProject(event.target.value)}
            value={context.selectedProject?.id ?? ""}
          >
            {context.projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        ) : (
          <span>
            {context.error ? "Projects unavailable." : "No project yet."}{" "}
            <Link href="/projects" prefetch={false}>
              Open project workspaces
            </Link>
          </span>
        )}
        <small>
          Selection is accepted only when it belongs to the signed-in project
          list.
        </small>
      </div>
    </div>
  );
}
