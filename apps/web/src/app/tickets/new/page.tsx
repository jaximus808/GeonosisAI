// TDM-74: ticket creation, the main entry point (CONTEXT.md user flow step 2).
import { NewTicketForm } from '@/components/tdm74/NewTicketForm';

export const metadata = { title: 'New ticket · GeonosisAI' };

export default function NewTicketPage() {
  return <NewTicketForm />;
}
