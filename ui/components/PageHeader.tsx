import type { ReactNode } from 'react';

export default function PageHeader({
  eyebrow, title, description, actions,
}: {
  eyebrow: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="page-head">
      <div>
        <div className="eyebrow">✦ {eyebrow}</div>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </section>
  );
}
