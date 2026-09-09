# frozen_string_literal: true

require 'rails_helper'

describe LdapPeriodicRecheckJob do
  # Person#account_active is forced by User#set_account_active based on this env var, so it
  # has to be set to actually get a disabled account out of the factory. LdapMembershipCheck is
  # also temporarily disabled during creation so config/initializers/ldap_activation_hook.rb's
  # before_create check doesn't itself resolve the account before the job under test runs.
  def create_disabled_person(**attrs)
    stub_const('ENV', ENV.to_hash.merge('DEVISE_NEW_ACCOUNT_INACTIVE' => 'true'))
    allow(LdapMembershipCheck).to receive(:enabled?).and_return(false)
    person = create(:person, **attrs)
    allow(LdapMembershipCheck).to receive(:enabled?).and_return(true)
    person
  end

  describe '#perform' do
    context 'when LDAP is not configured' do
      before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(false) }

      it 'does not touch any user' do
        active = create(:person, account_active: true, name_abbreviation: 'atv')
        disabled = create_disabled_person(name_abbreviation: 'dis')
        allow(LdapMembershipCheck).to receive(:enabled?).and_return(false)

        described_class.new.perform

        expect(active.reload.account_active).to be(true)
        expect(disabled.reload.account_active).to be(false)
      end
    end

    context 'when LDAP is configured' do
      before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(true) }

      it 'activates a disabled Person who now satisfies the filter' do
        user = create_disabled_person(name_abbreviation: 'jd')
        allow(LdapMembershipCheck).to receive(:members).and_return(Set['jd'])

        described_class.new.perform

        expect(user.reload.account_active).to be(true)
      end

      it 'disables an active Person who no longer satisfies the filter' do
        user = create(:person, account_active: true, name_abbreviation: 'jd')
        allow(LdapMembershipCheck).to receive(:members).and_return(Set.new)

        described_class.new.perform

        expect(user.reload.account_active).to be(false)
      end

      it 'leaves an active member and a disabled non-member untouched' do
        active_member = create(:person, account_active: true, name_abbreviation: 'act')
        disabled_non_member = create_disabled_person(name_abbreviation: 'dis')
        allow(LdapMembershipCheck).to receive(:members).and_return(Set['act'])

        described_class.new.perform

        expect(active_member.reload.account_active).to be(true)
        expect(disabled_non_member.reload.account_active).to be(false)
      end

      it 'only runs a single membership query for the whole sweep' do
        create(:person, account_active: true, name_abbreviation: 'aaa')
        create(:person, account_active: true, name_abbreviation: 'bbb')
        allow(LdapMembershipCheck).to receive(:members).and_return(Set['aaa', 'bbb'])

        described_class.new.perform

        expect(LdapMembershipCheck).to have_received(:members).once
      end

      it 'leaves every user untouched when the LDAP connection raises' do
        # Created before the disabled fixture below: create_disabled_person leaves ENV's
        # DEVISE_NEW_ACCOUNT_INACTIVE stubbed to 'true' for the rest of the example, which
        # would otherwise force this account inactive too via User#set_account_active.
        second = create(:person, account_active: true, name_abbreviation: 'bbb')
        first = create_disabled_person(name_abbreviation: 'aaa')
        allow(LdapMembershipCheck).to receive(:members).and_raise(Net::LDAP::Error, 'connection refused')

        described_class.new.perform

        expect(first.reload.account_active).to be(false)
        expect(second.reload.account_active).to be(true)
      end
    end
  end
end
