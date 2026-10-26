import { createContext, useContext } from "react";

export type User = { id: number; email: string; name: string; role: string; institution_id?: number; org?: string };
export type Auth = { user: User | null; signIn: (token: string, user: User) => void; signOut: () => void };
export const AuthCtx = createContext<Auth>({ user: null, signIn: () => {}, signOut: () => {} });
export const useAuth = () => useContext(AuthCtx);
