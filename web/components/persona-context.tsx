"use client";

// Which persona the browser is researching as. The signed HttpOnly cookie set by
// POST /api/session is the only authority: this context reads it back through GET
// /api/session and never stores the persona anywhere else. Persona selection is not
// authentication, but everything on the page belongs to one User: switching from one persona
// to another is a hard boundary, a full load of the start page, so no Brief, attached PDF,
// Run stream or Report of the previous persona survives. Choosing a first persona (none was
// selected) keeps the start page, so a Brief drafted before choosing is not lost; on any
// other page (a Run or Report opened by link) it reloads that page, which then loads as the
// chosen persona instead of staying on the "select a persona first" error.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getPersonas, getSession, selectPersona, type Persona } from "@/lib/api";
import { hardNavigate } from "@/lib/navigation";

type Status = "loading" | "ready" | "error";

interface PersonaContextValue {
  status: Status;
  error: string | null;
  personas: Persona[];
  current: Persona | null;
  select: (personaId: string) => Promise<void>;
  reload: () => void;
}

const PersonaContext = createContext<PersonaContextValue | null>(null);

export function PersonaProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [error, setError] = useState<string | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [current, setCurrent] = useState<Persona | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setStatus("loading");
    Promise.all([getPersonas(), getSession()])
      .then(([all, session]) => {
        if (!live) return;
        setPersonas(all);
        setCurrent(session);
        setError(null);
        setStatus("ready");
      })
      .catch((e: Error) => {
        if (!live) return;
        setError(e.message);
        setStatus("error");
      });
    return () => {
      live = false;
    };
  }, [attempt]);

  const select = useCallback(async (personaId: string) => {
    let selected: Persona;
    try {
      selected = await selectPersona(personaId);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    if (current && current.id !== selected.id) {
      hardNavigate("/");
      return;
    }
    if (!current && window.location.pathname !== "/") {
      hardNavigate(window.location.pathname + window.location.search);
      return;
    }
    setCurrent(selected);
    setError(null);
  }, [current]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  return (
    <PersonaContext.Provider value={{ status, error, personas, current, select, reload }}>
      {children}
    </PersonaContext.Provider>
  );
}

export function usePersona(): PersonaContextValue {
  const value = useContext(PersonaContext);
  if (!value) throw new Error("usePersona must be used inside PersonaProvider");
  return value;
}
