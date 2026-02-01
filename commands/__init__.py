from .say_hello import SayHelloCommand
from .login import LoginCommand
from .create_user import CreateUserCommand
from .refresh_token import RefreshTokenCommand
from .logout import LogoutCommand
from .me import MeCommand
from .upload_user_avatar import UploadUserAvatarCommand
from .generate_secure_download_link import GenerateSecureDownloadLinkCommand
from .add_role import AddRoleCommand
from .assign_role import AssignRoleToUserCommand
from .get_command_names import GetCommandNamesCommand
from .synch_permission import SyncPermissionCommand
from .studio.transform_template import TransformTemplateRenderCommand
from .github.register import RegisterGithubUserCommand
from .github.signin import GithubSignInCommand
from .github.start import GithubStartCommand
from .components.contents.create import CreateCompContentsWithUploadsCommand
from .components.contents.update import UpdateComponentWithUploadsCommand
from .components.contents.delete import DeleteCompContentCommand
from .components.contents.get import ContentGetCommand

from .tagging.delete import TagDeleteCommand
from .tagging.create import TagCreateCommand
from .tagging.list_active import TagListActiveCommand

from .components.layouts.create import CreateCompLayoutsWithUploadsCommand
from .components.layouts.delete import DeleteCompLayoutCommand
from .components.layouts.get import LayoutGetCommand
from .components.layouts.update import UpdateLayoutWithUploadsCommand

from .components.pages.create import CreateCompPagesWithUploadsCommand
from .components.pages.update import UpdatePageWithUploadsCommand
from .components.pages.delete import DeleteCompPageCommand
from .components.pages.get import PageGetCommand

from .components.authorizations.create import CreateCompAuthenticationsWithUploadsCommand
from .components.authorizations.update import UpdateCompAuthenticationsWithUploadsCommand
from .components.authorizations.delete import DeleteCompAuthenticationsCommand
from .components.authorizations.get import AuthenticationGetCommand

from .components.navigations.create import CreateCompNavigationsWithUploadsCommand
from .components.navigations.update import UpdateNavigationsWithUploadsCommand
from .components.navigations.get import NavigationGetCommand
from .components.navigations.delete import DeleteCompNavigationCommand

from .components.funnels.create import CreateFunnelWithUploadsCommand
from .components.funnels.update import UpdateFunnelWithUploadsCommand
from .components.funnels.delete import DeleteFunnelsCommand
from .components.funnels.get import FunnelGetCommand

from .projects.create import CreateProjectWithUploadsCommand
from .projects.update import UpdateProjectWithUploadsCommand
from .projects.get import ProjectGetCommand
from .projects.delete import DeleteProjectsCommand

from .components.connections.create import CreateCompConnectionsCommand
from .components.connections.delete import DeleteCompConnectionsCommand
from .components.connections.get import ConnectionsGetCommand
from .components.connections.preview import CompConnectionPreview
