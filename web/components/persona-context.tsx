"use client";

// Which persona the browser is researching as. The signed HttpOnly cookie set by
// POST /api/session is the only authority: this context reads it back through GET
// /api/session and never stores the persona anywhere else. Persona selection is not
// authentication.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getPersonas, getSession, selectPersona, type Persona } from "@/lib/api";

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
    try {
      setCurrent(await selectPersona(personaId));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

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
