import { SellerOfferView } from '@/components/offers/SellerOfferView';

// TDM-94: the dataset seller's page for one offer.
export default async function SellOfferPage({ params }: { params: Promise<{ offerId: string }> }) {
  const { offerId } = await params;
  return <SellerOfferView offerId={offerId} />;
}
